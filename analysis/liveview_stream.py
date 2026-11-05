#!/usr/bin/env python3
"""Sustain live view: replay the connect sequence, then keep polling while reading frames.

Phocus keeps one `op 4` poll in flight continuously. Without it the camera sends a
single preview frame and stops. This interleaves the poll with short 0x84 reads and
reports the frame rate and payload sizes actually achieved.

  python3 liveview_stream.py [--seconds N] [--save DIR]
"""
import os
import sys
import time

import hbctl
import liveview_replay as lr


def frames_from(buf):
    """Yield complete LIVE frames from a byte buffer; returns the remainder."""
    out = []
    while True:
        i = buf.find(b"LIVE")
        if i < 0:
            return out, b""
        buf = buf[i:]
        if len(buf) < 24:
            return out, buf
        plen = int.from_bytes(buf[12:16], "little")
        total = 24 + plen
        if len(buf) < total:
            return out, buf
        out.append(buf[:total])
        buf = buf[total:]


def main():
    seconds = 12.0
    if "--seconds" in sys.argv:
        seconds = float(sys.argv[sys.argv.index("--seconds") + 1])
    save = None
    if "--save" in sys.argv:
        save = sys.argv[sys.argv.index("--save") + 1]
        os.makedirs(save, exist_ok=True)

    seq = lr.load_sequence()
    print("replaying %d commands" % len(seq))
    cam = hbctl.Camera(timeout=800)
    try:
        for op, code, val in seq:
            try:
                cam.call(op, code, val, retries=0)
            except Exception:
                pass
            if op == 7:
                lr.drain(cam, 250)

        print("streaming for %.0fs with polling" % seconds)
        buf = b""
        got = []
        end = time.time() + seconds
        polls = 0
        while time.time() < end:
            try:
                cam.call(hbctl.OP_INDEXED, 0x0001, b"\x07\x00\x00\x00", retries=0)
                polls += 1
            except Exception:
                pass
            try:
                buf += bytes(cam.dev.read(hbctl.EP_DATA, 1 << 18, timeout=80))
            except Exception:
                pass
            new, buf = frames_from(buf)
            got.extend(new)

        print("polls=%d  frames=%d  (%.1f fps)" % (polls, len(got), len(got) / seconds))
        if got:
            sizes = [len(f) for f in got]
            print("frame size: min=%d max=%d avg=%d" % (min(sizes), max(sizes), sum(sizes)//len(sizes)))
            if save:
                for i, f in enumerate(got):
                    open(os.path.join(save, "f_%03d.bin" % i), "wb").write(f)
                print("saved %d frames to %s" % (len(got), save))
    finally:
        cam.close()


if __name__ == "__main__":
    main()
