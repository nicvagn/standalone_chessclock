#!/usr/bin/env bash

THIS_DIR=$(cd $(dirname "${BASH_SOURCE[0]}") && pwd)

. ~/dev/NicLink/pyenv_up.sh

python $THIS_DIR/standalone_chessclock.py
