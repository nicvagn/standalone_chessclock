#! /bin/python
#  chess_clock is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or ( at your option ) any later version.
#
#  chess_clock is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License along with chess_clock. If not, see <https://www.gnu.org/licenses/>.

import logging
import os
import sys
from datetime import datetime, timedelta
from threading import Event, Lock, Thread
from time import sleep

import berserk
import readchar
import serial
from berserk.exceptions import ResponseError


# exceptions
class NicLinkGameOver(Exception):
    """the game on NicLink is over"""

    def __init__(self, message):
        self.message = message


### logger stuff ###
logger = logging.getLogger("chess_clock")

consoleHandler = logging.StreamHandler(sys.stdout)

logger.setLevel(logging.DEBUG)
consoleHandler.setLevel(logging.DEBUG)
# logger.setLevel(logging.ERROR) for production
# consoleHandler.setLevel(logging.ERROR)

formatter = logging.Formatter("%(asctime)s %(levelname)s %(module)s %(message)s")

consoleHandler.setFormatter(formatter)
logger.addHandler(consoleHandler)

# logging to a file
fileHandler = logging.FileHandler("NicLink.log")
fileHandler.setLevel(logging.DEBUG)

logger.addHandler(fileHandler)


def log_handled_exception(exception) -> None:
    """log a handled exception"""
    global logger
    logger.error("Exception handled: %s", exception)


"""
snip from chess_clock.ino
  case '2':
    signalGameOver();
    break;
  case '3':
    // show a str on LED read from Serial
    printSerialMessage();
    break;
  case '4':
    // start a new game
    newGame();
    break;
  case '5':
    // show the splay
    niclink_splash();
    break;
  case '6':
    // white one, and the game is over
    white_won();
    break;
  case '7':
    // black won the game
    black_won();
    break;
  case '8':
    // game is a draw
    drawn_game();
    break;
  case '@':
    //say hello
    lcd.clear();
    lcd.setCursor(1, 0);
    lcd.print("Hi there");
    break;
"""


class ChessClock:
    """a controlling class to encapsulate and facilitate interaction's
    with Arduino chess clock. Starts the game time when this object is
    created

    Attributes
    ----------
        logger : logger
            self explanitory I think
        chess_clock : Serial
            the serial port that the niclink(tm) ardino
            chess clock is connected to
        lcd_length : int
            the char lingth of the lcd in chess clock
        displayed_wtime : timedelta
            the last recived white time delta from lila
        displayed_btime : timedelta
            the last recived black time delta from lila
        move_time : datetime | None
            time of the last move
        white_to_move : bool
            is it whites move?

    """

    def __init__(
        self,
        serial_port: str,
        baudrate: int,
        timeout: float,
        berserk_board_client=None,
        logger=None,
    ):  # , port="/dev/ttyACM0", baudrate=115200, timeout=100.0) -> None:
        """initialize connection with ardino, and record start time"""
        # the refresh rate of the lcd
        self.TIME_REFRESH = 0.5
        if logger is not None:
            self.logger = logger
        else:
            raise Exception("no logger")
        self.chess_clock = serial.Serial(
            port=serial_port, baudrate=baudrate, timeout=timeout
        )
        self.lcd_length = 16
        self.displayed_btime = None
        self.displayed_wtime = None
        self.countdown = None
        # event to signal white to move
        self.white_to_move = Event()

        # event to signal game over
        self.game_over_event = Event()

        # a lock for accessing the time vars
        self.time_lock = Lock()

        # time left for the player that moved, at move time
        self.time_left_at_move = None

        if berserk_board_client is None:
            raise Exception("No board client")

        self.berserk_board_client = berserk_board_client

        self.logger.info("ChessClock initialized")

    def move_made(self) -> None:
        """a move was made in the game this chess clock is for. HACK: Must be called
        before first move on game start before time_keeper is called
        """
        # record the move_time
        self.move_time = datetime.now()
        # record the time player has left at move time
        if self.white_to_move.is_set():
            with self.time_lock:
                self.time_left_at_move = self.displayed_wtime
            # clear event
            self.white_to_move.clear()
        else:
            with self.time_lock:
                self.time_left_at_move = self.displayed_btime
            # set white_to move
            self.white_to_move.set()

    def updateLCD(self, wtime: timedelta, btime: timedelta) -> None:
        """keep the external timer displaying correct time.
        The time stamp shuld be formated with both w and b timestamp set up to display
        correctly on a 16 x 2 LCD
        """
        timestamp = self.create_timestamp(wtime, btime)
        self.logger.info("\n\nTIMESTAMP: %s \n", timestamp)
        self.send_string(timestamp)

    def game_over(self, display_message=True) -> None:
        """Case 2: signal game over, w ASCII 2"""
        self.game_over_event.set()
        if display_message:
            self.chess_clock.write("2".encode("ascii"))

        if self.displayed_btime is not None and self.displayed_wtime is not None:
            self.logger.info(
                "ChessClock.game_over() entered w current ts: %s"
                % (self.create_timestamp(self.displayed_wtime, self.displayed_btime))
            )
        else:
            self.logger.warn(
                "ChessClock.game_over(): self.displayed_btime or self.displayed_wtime is None"
            )
        self.logger.info("ChessClock.game_over(...) called")

    def send_string(self, message: str) -> None:
        """Case 3: send a String to the external chess clock"""
        self.chess_clock.write("3".encode("ascii"))

        # tell the clock we want to display a msg
        self.chess_clock.write(message.encode("ascii"))

    def start_new_game(self, game_id) -> None:
        """Case 4: signal clock to start a new game
        reset all the game time data
        """
        logger.info("chess_clock should display '4': start_new_game")

        clock_not_initialized = True
        self.chess_clock.write("4".encode("ascii"))

        # white_to_move is true at begining of game
        self.white_to_move.set()

        # stream the incoming game events
        self.stream = self.berserk_board_client.stream_game_state(game_id)
        for event in self.stream:
            # HACK: if clock is not yet initialized, do that
            if clock_not_initialized:
                clock_not_initialized = False
                self.initialize_clock(event)
            logger.debug("event: %s", event)
            if event["type"] == "gameState":

                self.move_time = datetime.now()
                self.move_made()

    def show_splash(self) -> None:
        """Case 5: show the nl splash"""
        self.chess_clock.write("5".encode("ascii"))

    def white_won(self) -> None:
        """Case 6: show that white won"""
        self.chess_clock.write("6".encode("ascii"))
        self.game_over(display_message=False)

    def black_won(self) -> None:
        """Case 7: show that black won"""
        self.chess_clock.write("7".encode("ascii"))
        self.game_over(display_message=False)

    def drawn_game(self) -> None:
        """Case 8: show game is drawn"""
        self.chess_clock.write("8".encode("ascii"))
        self.game_over(display_message=False)

    def show_splash(self) -> None:
        """Case 5: show the nl splash"""
        self.chess_clock.write("5".encode("ascii"))

    def white_won(self) -> None:
        """Case 6: show that white won"""
        self.chess_clock.write("6".encode("ascii"))
        self.game_over(display_message=False)

    def black_won(self) -> None:
        """Case 7: show that black won"""
        self.chess_clock.write("7".encode("ascii"))
        self.game_over(display_message=False)

    def drawn_game(self) -> None:
        """Case 8: show game is drawn"""
        self.chess_clock.write("8".encode("ascii"))
        self.game_over(display_message=False)

    def initialize_clock(self, init_event) -> None:
        """initilize the clock. This involves reading the time from lila event
        and displaying the game time on the ext clock"""
        # make sure countown is exited
        if self.countdown is not None:
            if self.countdown.is_alive():
                raise Exception("ChessClock.countdown() is still alive")

        # reset clock var's
        self.move_time: datetime | None = None
        self.white_to_move.set()

        # last recived w and b time
        self.displayed_wtime = timedelta(milliseconds=init_event["state"]["wtime"])
        self.displayed_btime = timedelta(milliseconds=init_event["state"]["btime"])

        self.time_left_at_move = None
        # HACK: tell the clock that that the game started
        self.move_made()

        # start timekeeper thread
        self.countdown = Thread(target=self.time_keeper, args=(self,), daemon=True)
        self.countdown.start()

    def create_timestamp(self, wtime: timedelta, btime: timedelta) -> str:
        """create timestamp with white and black time for display on lcd"""
        # update the last received btime and wtime
        with self.time_lock:
            self.displayed_wtime = wtime
            self.displayed_btime = btime
        # ensure ts uses all the space, needed for lcd side
        white_time = f"W: { str(wtime) }"
        if len(white_time) > self.lcd_length:
            white_time = white_time[: self.lcd_length]
        else:
            while len(white_time) < self.lcd_length:
                white_time += " "

        black_time = f"B: { str(btime) }"
        if len(black_time) > self.lcd_length:
            black_time = black_time[: self.lcd_length]
        else:
            while len(black_time) < self.lcd_length:
                black_time += " "

        timestamp = f"{white_time}{black_time}"
        self.logger.info("ChessClock.chess_clock() created: %s" % (timestamp))
        print(timestamp)
        return timestamp

    @staticmethod
    def did_flag(player_time: timedelta) -> bool:
        """check if a timedelta is 0 total_seconds or less. ie: they flaged"""
        if player_time.total_seconds() <= 0:
            return True

        return False

    # TODO: make only update right time
    @staticmethod
    def time_keeper(chess_clock) -> None:
        """keep the time on the lcd correct. using the last time a move was made"""

        while True:
            # if the game is over, kill the time_keeper
            if chess_clock.game_over_event.is_set():
                raise NicLinkGameOver(
                    """time_keeper(...) exiting. 
chess_clock.game_over_event.is_set()"""
                )

            if chess_clock.move_time is None:
                sleep(chess_clock.TIME_REFRESH)
                continue
            if chess_clock.time_left_at_move is None:
                sleep(chess_clock.TIME_REFRESH)
                continue
            if chess_clock.displayed_btime is None:
                sleep(chess_clock.TIME_REFRESH)
                continue
            if chess_clock.displayed_wtime is None:
                sleep(chess_clock.TIME_REFRESH)
                continue

            # if it is white to move
            if chess_clock.white_to_move.is_set():
                # breakpoint()
                # create a new timedelta with the updated wtime
                new_wtime = chess_clock.time_left_at_move - (
                    datetime.now() - chess_clock.move_time
                )
                # check for flag for white
                if ChessClock.did_flag(new_wtime):
                    chess_clock.white_won()
                    raise NicLinkGameOver("white flaged")
                # update the clock
                chess_clock.updateLCD(new_wtime, chess_clock.displayed_btime)
            # else black to move
            else:
                # breakpoint()
                # create a new timedelta object w updated b time
                new_btime = chess_clock.time_left_at_move - (
                    datetime.now() - chess_clock.move_time
                )

                # check if black has flaged
                if ChessClock.did_flag(chess_clock.displayed_btime):
                    chess_clock.black_won()
                    raise NicLinkGameOver("black flaged")
                # update the clock
                chess_clock.updateLCD(chess_clock.displayed_btime, new_btime)

            sleep(chess_clock.TIME_REFRESH)


def handle_game_start(event, berserk_client, chess_clock: ChessClock) -> None:
    """handle game start event."""
    global logger
    # check for correspondance
    logger.info("handle_game_start(...) called")
    game_data = event["game"]

    if game_data["speed"] == "correspondence":
        logger.info("skipping correspondence game w/ id %s", game_data["id"])
        return

    if game_data["hasMoved"]:
        """handle ongoing game"""
        # TODO: handle_ongoing_game(game_data)

    # start the chess clock for this game
    chess_clock.start_new_game(game_data["id"])


# OLD
def test_chessclock(chess_clock) -> None:
    """test chess_clock functionality"""

    chess_clock.displayed_wtime = timedelta(seconds=9)
    chess_clock.displayed_btime = timedelta(seconds=9)
    # init game
    chess_clock.move_made()

    sleep(3)
    chess_clock.updateLCD(timedelta(minutes=1), timedelta(minutes=1))
    sleep(3)
    chess_clock.game_over()
    sleep(3)
    chess_clock.updateLCD(timedelta(hours=4, minutes=1), timedelta(hours=3, minutes=33))
    sleep(3)
    chess_clock.game_over()
    sleep(3)
    chess_clock.updateLCD(timedelta(minutes=4), timedelta(minutes=8))


def main() -> None:
    global logger
    PORT = "/dev/ttyACM0"
    BR = 115200  # baudrate for Serial connection
    REFRESH_DELAY = 100.0  # refresh delay for chess_clock
    script_dir = os.path.dirname(__file__)
    TOKEN_FILE = os.path.join(script_dir, "lichess_token/token")

    try:
        logger.info("reading token from %s", TOKEN_FILE)
        with open(TOKEN_FILE) as f:
            token = f.read().strip()

    except FileNotFoundError:
        print(f"ERROR: cannot find token file")
        sys.exit(-1)
    except PermissionError:
        print(f"ERROR: permission denied on token file")
        sys.exit(-1)

    try:
        session = berserk.TokenSession(token)
    except:
        e = sys.exc_info()[0]
        log_handled_exception(e)
        print(f"cannot create session: {e}")
        logger.info("cannot create session", e)
        sys.exit(-1)

    try:
        berserk_client = berserk.Client(session)
    except KeyboardInterrupt as err:
        log_handled_exception(err)
        print("KeyboardInterrupt: bye")
        sys.exit(0)
    except:
        e = sys.exc_info()[0]
        error_txt = f"cannot create lichess client: {e}"
        logger.info(error_txt)
        print(error_txt)
        sys.exit(-1)

    # get username
    try:
        account_info = berserk_client.account.get()
        username = account_info["username"]
        print(f"\nUSERNAME: { username }\n")
    except KeyboardInterrupt:
        print("KeyboardInterrupt: bye")
        sys.exit(0)
    except:
        e = sys.exc_info()[0]
        logger.info("cannot get lichess acount info: %s", e)
        print(f"cannot get lichess acount info: {e}")
        sys.exit(-1)

    chess_clock = ChessClock(
        PORT,
        BR,
        REFRESH_DELAY,
        berserk_board_client=berserk_client.board,
        logger=logger,
    )

    # test_chessclock(chess_clock)
    # main program loop
    while True:
        try:
            logger.debug("\n==== event loop ====\n")
            print("=== Waiting for lichess event ===")
            for event in berserk_client.board.stream_incoming_events():
                if event["type"] == "challenge":
                    logger.info("challenge received: %s", event)
                    print("\n==== Challenge received ====\n")
                    print(event)
                elif event["type"] == "gameStart":
                    # a game is starting, it is handled by a function
                    handle_game_start(event, berserk_client.board, chess_clock)
                """
                elif event["type"] == "gameFull":
                    nl_inst.game_over.set()
                    handle_resign(event)
                    print("GAME FULL received")
                    logger.info("\ngameFull received\n")

                # check for kill switch
                if nl_inst.kill_switch.is_set():
                    sys.exit(0)
                """

        except ResponseError as e:
            print(f"ERROR: Invalid server response: {e}")
            logger.info("Invalid server response: %s", e)
            if "Too Many Requests for url" in str(e):
                sleep(10)

        sleep(5)


if __name__ == "__main__":
    main()
