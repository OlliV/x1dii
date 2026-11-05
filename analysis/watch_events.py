#!/usr/bin/env python3
"""List asynchronous events (opcode 0x40) captured in a libusb trace.

The camera pushes a notification whenever a parameter changes for a reason the host did
not cause — a dial turned, a mode switched, a menu item chosen on the camera body. That
makes the trace a decoder ring for parameter writes: change the setting on the camera and
the frame it produces tells you exactly which code and encoding describes it.

Usage:  python3 watch_events.py [-f /tmp/libusb_trace.log] [--tail N]
"""
import argparse
import re
import sys

REPLY = re.compile(r"<- data:(03 00 05 08 .*)$")
REQ = re.compile(r"-> data:(04 00 08 05 .*)$")

NAMES = {}


def load_names():
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parameter-table.txt")
    try:
        for line in open(path):
            m = re.match(r"\s*(0x[0-9a-fA-F]{4})\s+(\S+)", line)
            if m:
                NAMES[int(m.group(1), 16)] = m.group(2)
    except OSError:
        pass


def frames(path):
    """Yield (kind, seq, op, data) for every well-framed command/reply in the trace."""
    for line in open(path, errors="replace"):
        m = REPLY.search(line.strip())
        magic = 0x03
        if not m:
            m = REQ.search(line.strip())
            magic = 0x04
        if not m:
            continue
        b = [int(x, 16) for x in m.group(1).split()]
        n = b[4]
        pay = b[5:5 + n]
        if len(pay) < 3:
            continue
        seq = int.from_bytes(bytes(pay[:2]), "little")
        op = pay[2]
        yield ("reply" if magic == 0x03 else "request"), seq, op, bytes(pay[3:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--file", default="/tmp/libusb_trace.log")
    ap.add_argument("--tail", type=int, default=40)
    ap.add_argument("--all", action="store_true", help="also list request context")
    a = ap.parse_args()

    load_names()
    events = []
    for kind, seq, op, data in frames(a.file):
        if kind == "reply" and op == 0x40:
            events.append((seq, data))

    print("%d event(s)\n" % len(events))
    for seq, data in events[-a.tail:]:
        # an event body looks like <code u16> <value>  (inferred from observed records)
        code = int.from_bytes(data[:2], "little") if len(data) >= 2 else None
        name = NAMES.get(code, "") if code is not None else ""
        text = data[2:].decode("latin-1", "replace")
        printable = "".join(c if 32 <= ord(c) < 127 else "." for c in text)
        print("seq=0x%04x  %s" % (seq, data.hex(" ")))
        if code is not None:
            print("           code=0x%04x %-20s rest=%r" % (code, name, text))
            print("           ascii: %s" % printable)


if __name__ == "__main__":
    main()
