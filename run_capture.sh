#!/bin/sh
# Start the X1D II tethering app.
# Video opens in an ffplay window; the controls are a small Tk window.
cd "$(dirname "$0")" || exit 1
exec python3 capture/viewer.py "$@"
