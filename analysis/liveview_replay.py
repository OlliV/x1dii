#!/usr/bin/env python3
"""Replay the command sequence Phocus sends before live view starts.

The camera streams preview frames on 0x84 only once something has put it into the
right state. That something is in `trace-liveview-2064.log`: 181 commands preceding
the first LIVE frame. This replays them from hbctl and then listens on 0x84.

The sequence contains no capture trigger, so replaying it cannot fire the shutter.
It does contain Phocus's own parameter writes (TetheredMode, WhiteBalance, mirror),
which are the same ones the app issues on connect.

  python3 liveview_replay.py [--dry-run] [--grab N]
"""
import os
import re
import struct
import sys
import time

import hbctl

TRACE = "analysis/trace-liveview-2064.log"
FIRST_LIVE = 1793860712.8            # timestamp of the first LIVE frame
DRAIN = {7: 300}                     # op -> ms to allow a 0x84 drain afterwards


def load_sequence(path=TRACE):
    seq = []
    for line in open(path, errors="replace"):
        m = re.search(r"([\d.]+)\s+-> data:(04 00 08 05 .*)$", line.strip())
        if not m:
            continue
        if float(m.group(1)) > FIRST_LIVE:
            break
        b = [int(x, 16) for x in m.group(2).split()]
        payload = bytes(b[5:5 + b[4]])
        seq.append((payload[2], int.from_bytes(payload[3:5], "little"), payload[5:]))
    return seq


def drain(cam, ms=200):
    """Swallow any queued bulk data so it cannot be mistaken for a reply."""
    got = 0
    end = time.time() + ms / 1000.0
    while time.time() < end:
        try:
            b = bytes(cam.dev.read(hbctl.EP_DATA, 1 << 20, timeout=60))
        except Exception:
            break
        if not b:
            break
        got += len(b)
    return got


def main():
    dry = "--dry-run" in sys.argv
    grab = 3
    if "--grab" in sys.argv:
        grab = int(sys.argv[sys.argv.index("--grab") + 1])
    save = None
    if "--save" in sys.argv:
        save = sys.argv[sys.argv.index("--save") + 1]

    seq = load_sequence()
    print("replaying %d commands" % len(seq))
    if dry:
        for op, code, val in seq:
            print("  op%d 0x%04x %s" % (op, code, val.hex(" ")))
        return

    cam = hbctl.Camera(timeout=800)
    live = 0
    try:
        for i, (op, code, val) in enumerate(seq):
            try:
                cam.call(op, code, val, retries=0)
            except Exception as e:
                print("  cmd %d (op%d 0x%04x): %s" % (i, op, code, type(e).__name__))
            if op in DRAIN:
                drain(cam, DRAIN[op])
            if i % 40 == 0:
                print("  ... %d/%d" % (i, len(seq)))

        print("sequence done; listening on 0x84")
        end = time.time() + 25
        buf = b""
        while time.time() < end and live < grab:
            try:
                chunk = bytes(cam.dev.read(hbctl.EP_DATA, 1 << 20, timeout=1200))
            except Exception:
                continue
            buf += chunk
            while len(buf) >= 24 and buf[:4] == b"LIVE":
                w = int.from_bytes(buf[16:20], "little")
                h = int.from_bytes(buf[20:24], "little")
                plen = int.from_bytes(buf[12:16], "little")
                total = 24 + plen
                if len(buf) < total:
                    break                       # need more of this frame
                frame, buf = buf[:total], buf[total:]
                body = frame[24:]
                magic = body[:4].hex(" ")
                kind = "JPEG" if body[:2] == b"\xff\xd8" else "?"
                print("  LIVE %dx%d payload=%d kind=%s magic=%s" % (w, h, plen, kind, magic))
                if save:
                    os.makedirs(save, exist_ok=True)
                    fn = os.path.join(save, "frame_%03d.bin" % live)
                    open(fn, "wb").write(frame)
                    print("    saved %s (%d bytes)" % (fn, len(frame)))
                live += 1
                if live >= grab:
                    break
            if buf and len(buf) < 24 and buf[:4] != b"LIVE":
                buf = b""
    finally:
        cam.close()
    print("\n%d LIVE frame(s) seen" % live)


if __name__ == "__main__":
    main()
