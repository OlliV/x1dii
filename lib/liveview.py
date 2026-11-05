#!/usr/bin/env python3
"""Live view helpers shared by the capture apps and tools.

  frames_from(buf)   pull complete video frames out of a byte buffer, dropping the
                     1 KB control packets the camera interleaves on the same channel
  load_sequence()    the 181 commands Phocus sends before live view starts
  drain(cam, ms)     swallow queued bulk data so it is not mistaken for a reply

Nothing here talks to the camera except `drain`, which takes a Camera from hbctl.
"""
import os
import sys
import re
import time

from . import hbctl


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACE = os.path.join(ROOT, "analysis", "trace-liveview-2064.log")
FIRST_LIVE = 1793860712.8            # timestamp of the first LIVE frame in the trace


def load_sequence(path=None):
    """The commands Phocus sends before live view starts, as (op, code, value)."""
    path = path or TRACE
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
    """Swallow queued bulk data so it cannot be mistaken for a reply."""
    end = time.time() + ms / 1000.0
    while time.time() < end:
        try:
            if not cam.dev.read(0x84, 1 << 20, timeout=60):
                break
        except Exception:
            break


def stream_parse(buf):
    """Split the channel into video frames and control packets.

    Returns (frames, events, remainder). One endpoint carries both: large H.264 frames
    beginning "LIVE", and control packets `03 00 05 08 <len> <payload>`. Parsing them
    together matters because a camera-initiated capture arrives as a control packet
    interleaved with video - a reader that only looks for "LIVE" discards it.
    """
    frames, events = [], []
    while buf:
        if buf.startswith(b"\x03\x00\x05\x08"):
            if len(buf) < 5:
                break
            n = buf[4]
            if len(buf) < 5 + n:
                break
            events.append(buf[5:5 + n])
            buf = buf[5 + n:]
        elif buf.startswith(b"LIVE"):
            if len(buf) < 24:
                break
            plen = int.from_bytes(buf[12:16], "little")
            if plen <= 0 or plen > (1 << 22):
                buf = buf[4:]
                continue
            if len(buf) < 24 + plen:
                break
            frames.append(buf[:24 + plen])
            buf = buf[24 + plen:]
        else:
            buf = buf[1:]                 # resync to the next packet boundary
    return frames, events, buf


def frames_from(buf):
    """Pull every complete video frame out of a byte buffer.

    Returns (frames, remainder). Two things this must do, both learned the hard way:

    * the channel interleaves 1 KB control packets carrying the same `LIVE` magic but
      width = height = 0; feeding those to a decoder corrupts it, so they are dropped
      here rather than by every caller
    * a bogus length would otherwise make us wait forever for bytes that never come

    This is the single canonical parser; viewer.py and liveapp.py both use it.
    """
    out = []
    while True:
        i = buf.find(b"LIVE")
        if i < 0:
            return out, b""
        if i:
            buf = buf[i:]
        if len(buf) < 24:
            return out, buf
        plen = int.from_bytes(buf[12:16], "little")
        if plen <= 0 or plen > (1 << 22):
            buf = buf[4:]                     # bad length: resync past this magic
            continue
        if len(buf) < 24 + plen:
            return out, buf
        frame, buf = buf[:24 + plen], buf[24 + plen:]
        w = int.from_bytes(frame[16:20], "little")
        h = int.from_bytes(frame[20:24], "little")
        if w and h:
            out.append(frame)                 # skip control packets
