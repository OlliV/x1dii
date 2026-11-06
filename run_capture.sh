#!/bin/sh
# Start the X1D II tethering app.
# Video opens in an ffplay window; the controls are a small Tk window.
#
# Uses the repo's .venv when there is one. Create it with a Python whose Tk is 9 (or at
# least 8.6.13) - Tk 8.6.12 drops mouse clicks while the app is not the active one:
#   brew install python-tk@3.13
#   /opt/homebrew/bin/python3.13 -m venv .venv && .venv/bin/python -m pip install pyusb pillow numpy
cd "$(dirname "$0")" || exit 1
PYTHON=python3
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python
exec "$PYTHON" capture/viewer.py "$@"
