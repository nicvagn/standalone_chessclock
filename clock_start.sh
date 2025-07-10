#!/usr/bin/env bash

THIS_DIR=$(cd $(dirname "${BASH_SOURCE[0]}") && pwd)

. ~/git/NicLink/.env/venv/bin/activate

python $THIS_DIR/standalone_chessclock.py
