#!/usr/bin/env python3
"""Replay the connect sequence Phocus sends and see whether live view starts.

The camera streams only after this sequence. One command in it is the actual trigger
(op 1 / 0x0007 / first byte 0x0c), but replaying the whole thing is also what puts the
body into tether-transfer mode, which downloads need.

  python3 tools/liveview_replay.py --dry-run     # show the sequence
  python3 tools/liveview_replay.py --grab 3      # replay, then grab frames
  python3 tools/liveview_replay.py --grab 3 --save /tmp/live
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import hbctl                              # noqa: E402
from lib import liveview as lv                     # noqa: E402


def main():
    seq = lv.load_sequence()
    print("sequence: %d commands" % len(seq))

    if "--dry-run" in sys.argv:
        for op, code, val in seq:
            print("  op%d 0x%04x %s" % (op, code, val.hex(" ")))
        return

    grab = 3
    if "--grab" in sys.argv:
        grab = int(sys.argv[sys.argv.index("--grab") + 1])
    save = None
    if "--save" in sys.argv:
        save = sys.argv[sys.argv.index("--save") + 1]
        os.makedirs(save, exist_ok=True)

    cam = hbctl.Camera(timeout=800)
    got = 0
    try:
        for i, (op, code, val) in enumerate(seq):
            try:
                cam.call(op, code, val, retries=0)
            except Exception:
                pass
            if op == 7:
                lv.drain(cam, 200)
            if i % 40 == 0:
                print("  ... %d/%d" % (i, len(seq)))

        print("replay done; listening on 0x84")
        buf = b""
        end = time.time() + 20
        while time.time() < end and got < grab:
            try:
                buf += bytes(cam.dev.read(hbctl.EP_DATA, 1 << 20, timeout=1200))
            except Exception:
                continue
            frames, buf = lv.frames_from(buf)
            for frame in frames:
                w = int.from_bytes(frame[16:20], "little")
                h = int.from_bytes(frame[20:24], "little")
                print("  LIVE %dx%d payload=%d" % (w, h, len(frame) - 24))
                if save:
                    path = os.path.join(save, "frame_%03d.bin" % got)
                    open(path, "wb").write(frame)
                    print("    saved %s" % path)
                got += 1
                if got >= grab:
                    break
    finally:
        cam.close()
    print("%d frame(s)" % got)


if __name__ == "__main__":
    main()
