#!/usr/bin/env python3
"""Live view helpers shared by the capture apps and tools.

  frames_from(buf)   pull complete video frames out of a byte buffer, dropping the
                     1 KB control packets the camera interleaves on the same channel
  load_sequence()    the 181 commands Phocus sends before live view starts
  drain(cam, ms)     swallow queued bulk data so it is not mistaken for a reply

Nothing here talks to the camera except `drain`, which takes a Camera from hbctl.
"""
import json
import os
import re
import time

from . import hbctl


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACE = os.path.join(ROOT, "analysis", "trace-liveview-2064.log")
# The parsed command list, checked in so the app bundle does not have to carry a 2 MB
# trace file. The trace is the source of truth; session_sequence.json is derived from it.
SEQUENCE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "session_sequence.json")
FIRST_LIVE = 1793860712.8            # timestamp of the first LIVE frame in the trace

# A LIVE packet: "LIVE", u32 4, u32 1024, u32 LENGTH, u32 width, u32 height, payload.
# LENGTH is the whole packet *including* this 24-byte header - one bulk transfer is one
# packet, and its size equals LENGTH (checked on 1258 frames in Phocus's trace and on the
# live camera). Reading it as the payload length, as this code once did, makes every
# packet look 24 bytes longer than it is: the parser then eats the next packet's header,
# skips that frame entirely, and hands the decoder half the frames with junk appended -
# 11 fps of smeared motion where the camera sends 22.
LIVE_HEADER = 24
LIVE_MAX = 1 << 23


def _live_length(buf, i):
    """Total length of the LIVE packet at buf[i], or None if the field is implausible."""
    total = int.from_bytes(buf[i + 12:i + 16], "little")
    return total if LIVE_HEADER < total <= LIVE_MAX else None


def payload(frame):
    """The H.264 bytes of a complete LIVE packet."""
    return frame[LIVE_HEADER:]


def load_sequence(path=None):
    """The commands Phocus sends before live view starts, as (op, code, value).

    Reads the checked-in JSON when present so the app bundle works without the trace,
    and falls back to decoding the trace when run from a checkout that has it.
    """
    if path is None and os.path.exists(SEQUENCE):
        with open(SEQUENCE) as fh:
            return [(op, code, bytes.fromhex(val)) for op, code, val in json.load(fh)]
    path = path or TRACE
    seq = []
    with open(path, errors="replace") as fh:
        for line in fh:
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
            if not cam.dev.read(hbctl.EP_DATA, 1 << 20, timeout=60):
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
    i, n = 0, len(buf)
    while i < n:
        if buf.startswith(hbctl.HDR_IN, i):
            if n - i < 5:
                break
            end = i + 5 + buf[i + 4]
            if end > n:
                break
            events.append(buf[i + 5:end])
            i = end
        elif buf.startswith(b"LIVE", i):
            if n - i < LIVE_HEADER:
                break
            total = _live_length(buf, i)
            if total is None:
                i += 4                    # bad length: resync past this magic
                continue
            if n - i < total:
                break
            frames.append(buf[i:i + total])
            i += total
        else:
            # Not at a packet: jump to the next header rather than stepping a byte at a
            # time. Anything else on the channel - a stray file transfer is megabytes -
            # would otherwise cost a full copy of the buffer per byte skipped.
            nxt = [j for j in (buf.find(hbctl.HDR_IN, i), buf.find(b"LIVE", i)) if j >= 0]
            if nxt:
                i = min(nxt)
            else:
                i = max(i, n - 3)         # keep a header split across two reads
                break
    return frames, events, buf[i:]


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
        if len(buf) < LIVE_HEADER:
            return out, buf
        total = _live_length(buf, 0)
        if total is None:
            buf = buf[4:]                     # bad length: resync past this magic
            continue
        if len(buf) < total:
            return out, buf
        frame, buf = buf[:total], buf[total:]
        w = int.from_bytes(frame[16:20], "little")
        h = int.from_bytes(frame[20:24], "little")
        if w and h:
            out.append(frame)                 # skip control packets
