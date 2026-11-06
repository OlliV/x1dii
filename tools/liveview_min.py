#!/usr/bin/env python3
"""Find the shortest prefix of Phocus's connect sequence that starts live view.

For a given prefix length N we send the first N commands, then poll-and-read for a few
seconds. If LIVE frames arrive, that prefix is sufficient.

The camera keeps streaming once started, so each trial reports a baseline first: if
frames are already flowing before we send anything, the previous trial is still
bleeding into this one and the result is marked unreliable.

  python3 liveview_min.py 20 40 60 80 120 181
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import hbctl              # noqa: E402
from lib import liveview as ls     # noqa: E402
from lib.liveview import load_sequence, drain   # noqa: E402


def trial(n, seq, sustain=2.5, label=""):
    cam = hbctl.Camera(timeout=800)
    try:
        # baseline: is it already streaming from a previous trial?
        base = 0
        try:
            b = bytes(cam.dev.read(hbctl.EP_DATA, 1 << 18, timeout=250))
            if b[:4] == b"LIVE":
                base = 1
        except Exception:
            pass

        for op, code, val in seq[:n]:
            try:
                cam.call(op, code, val, retries=0)
            except Exception:
                pass
            if op == 7:
                drain(cam, 200)

        buf = b""
        frames = 0
        end = time.time() + sustain
        while time.time() < end:
            try:
                cam.call(hbctl.OP_INDEXED, 0x0001, b"\x07\x00\x00\x00", retries=0)
            except Exception:
                pass
            try:
                buf += bytes(cam.dev.read(hbctl.EP_DATA, 1 << 18, timeout=80))
            except Exception:
                pass
            new, buf = ls.frames_from(buf)
            frames += len(new)
        return frames, base
    finally:
        cam.close()
        time.sleep(1.0)


def main():
    seq = load_sequence()
    ns = [int(a) for a in sys.argv[1:]] or [181]
    for n in ns:
        if n > len(seq):
            continue
        frames, base = trial(n, seq)
        flag = ""
        if base:
            flag = "  (baseline was streaming - unreliable)"
        verdict = "STREAMING" if frames else "no"
        print("N=%3d  frames=%3d  %s%s" % (n, frames, verdict, flag), flush=True)

    # settle: make sure we are not leaving a stream running
    time.sleep(1.0)
    print("done")


if __name__ == "__main__":
    main()
