#!/usr/bin/env python3
"""hbctl - host driver for the Hasselblad X1D II over USB vendor interface 3.

Protocol recovered by tracing Phocus 4.1.1's libusb traffic; see PROTOCOL.md.

  host -> camera   ep 0x04     04 00 08 05 <n> <payload n>
  camera -> host   ep 0x85     03 00 05 08 <n> <payload n>
  camera -> host   ep 0x84     bulk data (live view, storage blocks)

  payload       = <seq u16 LE> <body>
  request body  = <op u8> <code u16 LE> [value]
  reply body    = <0x80|op u8> <data>          (0x40 in that slot = async event)

Opcodes
  1  command / trigger     5  list parameter codes
  2  read value            6  describe parameter
  3  write value           7  data block (storage)

Values are typed ASCII in both directions:
  i<n>            integer             I<count>,<v1>,..   integer list / enum
  f<16 hex>       double, big-endian  F<count>,<hex..>   double list
  S<len>,<text>   string              r<a,b,c>          packed rational

Writes go out as op 3 using the same `I<n>,<values>` form the camera reads back.

Commands
  hbctl.py list                       enumerate advertised parameters
  hbctl.py read ExposureTime Iso      read one or more
  hbctl.py write Iso 400              write, then verify by read-back
  hbctl.py describe LiveVideoMode     descriptor (range / enum / table)
  hbctl.py dump                       everything: value + descriptor
  hbctl.py watch Iso Battery          poll and print changes
  hbctl.py snapshot out.json          full state as JSON
  hbctl.py storage                    list the card
  hbctl.py raw <op> <code> [hex]      arbitrary frame (writes need --write)

Nothing here touches persistent state: every command is one the camera's own
software issues, and `write` always reports the read-back, so a rejected value is
visible rather than silent.
"""
import json
import os
import re
import struct
import sys
import time

try:
    import usb.core
    import usb.util
except ImportError:
    sys.path.insert(0, "/tmp/pylibs")
    import usb.core
    import usb.util

VID, PID = 0x2756, 0x0007
IFACE = 3
EP_OUT, EP_IN, EP_DATA = 0x04, 0x85, 0x84

OP_CMD, OP_READ, OP_WRITE, OP_INDEXED, OP_LIST, OP_DESCRIBE, OP_BLOCK = 1, 2, 3, 4, 5, 6, 7

NAMES = {}


def _load_table():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parameter-table.txt")
    try:
        for line in open(path):
            m = re.match(r"\s*(0x[0-9a-fA-F]{4})\s+(\S+)", line)
            if m:
                NAMES[int(m.group(1), 16)] = m.group(2)
    except OSError:
        pass


_load_table()


def name_of(code):
    return NAMES.get(code, "")


def resolve(text):
    t = str(text).lower()
    if t.startswith("0x"):
        return int(t, 16)
    for k, v in NAMES.items():
        if v.lower() == t:
            return k
    try:
        return int(t, 0)
    except ValueError:
        raise SystemExit("unknown parameter %r (try `list`)" % text)


# ---------------------------------------------------------------- values

def decode_typed(d):
    """Decode a module-2 reply into (tag, payload)."""
    if not d:
        return None, None
    tag = chr(d[0])
    rest = d[1:]
    try:
        if tag == "i":
            return "i", int(rest.decode())
        if tag == "f":
            return "f", struct.unpack(">d", bytes.fromhex(rest.decode().rjust(16, "0")))[0]
        if tag == "S":
            n, _, s = rest.decode("latin-1").partition(",")
            return "S", s
        if tag == "I":
            parts = rest.decode("latin-1").split(",")
            return "I", [int(x) for x in parts[1:] if x != ""]
        if tag == "r":
            return "r", rest.decode("latin-1")
    except Exception:
        pass
    return tag, rest.decode("latin-1", "replace")


def encode_typed(tag, value):
    """Encode a Python value into the camera's typed-string form."""
    s = str(value)
    if len(s) > 1 and s[0] in "ISfr" and "," in s:
        return s.encode()                      # caller supplied the typed form
    if tag == "i":
        return ("I1,%d" % int(value)).encode()
    if tag == "f":
        return ("f" + struct.pack(">d", float(value)).hex()).encode()
    if tag == "I":
        vals = value if isinstance(value, (list, tuple)) else [value]
        return ("I%d,%s" % (len(vals), ",".join(str(int(v)) for v in vals))).encode()
    if tag == "r":
        return ("r%s" % s).encode()
    if tag == "S":
        return ("S%d,%s" % (len(s), s)).encode()
    return s.encode()


# ---------------------------------------------------------------- transport

class Camera:
    def __init__(self, timeout=1500):
        self.timeout = timeout
        self.seq = int(time.time() * 1000) & 0x3FFF
        self.dev = usb.core.find(idVendor=VID, idProduct=PID)
        if self.dev is None:
            raise SystemExit("camera not found (VID %04x PID %04x)" % (VID, PID))
        self.cfg = self.dev.get_active_configuration()
        self.intf = self.cfg[(IFACE, 0)]
        usb.util.claim_interface(self.dev, IFACE)

    def close(self):
        try:
            usb.util.release_interface(self.dev, IFACE)
        except Exception:
            pass
        usb.util.dispose_resources(self.dev)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _recv(self, want_seq=None, deadline=2.5):
        end = time.time() + deadline
        while time.time() < end:
            try:
                data = self.dev.read(EP_IN, 1024, timeout=self.timeout)
            except usb.core.USBTimeoutError:
                return None
            b = bytes(data)
            if len(b) < 7 or b[0] != 0x03 or b[1] != 0x00:
                continue
            payload = b[5:5 + b[4]]
            if len(payload) < 3:
                continue
            seq = struct.unpack("<H", payload[:2])[0]
            status = payload[2]
            if status == 0x40:
                continue                        # async event, not an answer
            if want_seq is not None and seq != want_seq:
                continue
            return status, payload[3:]
        return None

    def call(self, op, code, value=b"", retries=1):
        self.seq = (self.seq + 1) & 0x7FFF
        seq = self.seq
        body = bytes([op]) + struct.pack("<H", code) + value
        payload = struct.pack("<H", seq) + body
        frame = bytes([0x04, 0x00, 0x08, 0x05, len(payload)]) + payload
        for _ in range(retries + 1):
            self.dev.write(EP_OUT, frame, timeout=self.timeout)
            r = self._recv(want_seq=seq)
            if r is not None:
                return r
        return None

    # ---- high level

    def list_codes(self):
        r = self.call(OP_LIST, 0)
        if not r:
            return []
        data = r[1]
        return [struct.unpack("<H", data[i:i + 2])[0] for i in range(0, len(data) - 1, 2)]

    def read_raw(self, code):
        return self.call(OP_READ, code)

    def read(self, code):
        r = self.read_raw(code)
        if not r or not r[1]:
            return None
        return decode_typed(r[1])[1]

    def describe(self, code):
        r = self.call(OP_DESCRIBE, code)
        if not r or len(r[1]) < 3:
            return None
        return r[1][2:].decode("latin-1", "replace")

    def write(self, code, value):
        """Write and verify. Returns (applied, before, after, ack)."""
        raw = self.read_raw(code)
        tag, before = decode_typed(raw[1]) if raw else (None, None)
        payload = encode_typed(tag, value)
        r = self.call(OP_WRITE, code, payload)
        ack = r[1].hex(" ") if r else "no reply"
        time.sleep(0.05)
        after = self.read(code)
        try:
            applied = abs(float(after) - float(value)) < 1e-9
        except (TypeError, ValueError):
            applied = str(after) == str(value)
        return applied, before, after, ack

    def storage(self, expected=2770004):
        """List the card. The blob arrives on 0x84, possibly in several reads."""
        r = self.call(OP_BLOCK, 0x0009, bytes.fromhex("0000000000000000000054442a00"))
        if not r or r[1] != b"\x00\x00\x00\x00":
            return None
        blob = b""
        while len(blob) < expected:
            try:
                chunk = bytes(self.dev.read(EP_DATA, min(expected - len(blob), 1 << 20),
                                            timeout=1500))
            except usb.core.USBTimeoutError:
                break
            if not chunk:
                break
            blob += chunk
        if not blob.startswith(b"\x09"):
            return None                     # not a listing blob
        out, i = [], 25
        while i < len(blob) - 1:
            n = blob[i]
            if 3 <= n <= 120 and blob[i + 1:i + 2] == b"/":
                s2 = blob[i + 1:i + 1 + n]
                if all(32 <= c < 127 for c in s2):
                    out.append(s2.decode())
                    i += 1 + n
                    continue
            i += 1
        return out


# ---------------------------------------------------------------- cli

def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return
    allow_write = "--write" in argv
    argv = [a for a in argv if a != "--write"]
    cmd, rest = argv[0], [a for a in argv[1:] if not a.startswith("--")]

    with Camera() as cam:
        if cmd == "list":
            codes = cam.list_codes()
            print("%d advertised parameters" % len(codes))
            for c in codes:
                print("  0x%04x %s" % (c, name_of(c)))

        elif cmd == "read":
            for a in rest:
                c = resolve(a)
                print("0x%04x %-22s %r" % (c, name_of(c), cam.read(c)))

        elif cmd == "write":
            if len(rest) < 2:
                raise SystemExit("usage: write <parameter> <value>")
            c = resolve(rest[0])
            value = " ".join(rest[1:])
            for conv in (int, float):
                try:
                    value = conv(value)
                    break
                except ValueError:
                    pass
            applied, before, after, ack = cam.write(c, value)
            print("%-22s %r -> %r   (was %r, ack %s)%s" % (
                name_of(c) or hex(c), value, after, before, ack,
                "" if applied else "   NOT APPLIED"))

        elif cmd == "describe":
            for a in rest:
                c = resolve(a)
                print("0x%04x %-22s %s" % (c, name_of(c), cam.describe(c) or "-"))

        elif cmd == "dump":
            codes = cam.list_codes()
            print("%d parameters\n" % len(codes))
            for c in codes:
                print("0x%04x %-24s = %-30r %s" % (
                    c, name_of(c), cam.read(c), cam.describe(c) or ""))

        elif cmd == "watch":
            codes = [resolve(a) for a in rest] or [0x002f, 0x0008, 0x0009, 0x000a]
            last = {}
            print("watching %s  (ctrl-c to stop)"
                  % ", ".join(name_of(c) or hex(c) for c in codes))
            try:
                while True:
                    for c in codes:
                        v = cam.read(c)
                        if c in last and last[c] != v:
                            print("%s  %-20s %r -> %r" % (
                                time.strftime("%H:%M:%S"), name_of(c) or hex(c), last[c], v))
                        last[c] = v
                    time.sleep(0.4)
            except KeyboardInterrupt:
                print()

        elif cmd == "snapshot":
            out = rest[0] if rest else "snapshot.json"
            state = {}
            for c in cam.list_codes():
                key = "0x%04x %s" % (c, name_of(c)) if name_of(c) else "0x%04x" % c
                state[key] = {
                    "code": "0x%04x" % c,
                    "value": cam.read(c),
                    "descriptor": cam.describe(c),
                }
            json.dump(state, open(out, "w"), indent=1, default=str)
            print("wrote %s (%d parameters)" % (out, len(state)))

        elif cmd == "storage":
            paths = cam.storage()
            if not paths:
                print("no listing returned")
            else:
                print("%d entries" % len(paths))
                for p in paths:
                    print("   ", p)

        elif cmd == "raw":
            if len(rest) < 2:
                raise SystemExit("usage: raw <op> <code> [hexbytes]")
            op = int(rest[0], 0)
            c = resolve(rest[1])
            val = bytes.fromhex(rest[2]) if len(rest) > 2 else b""
            if not allow_write and op in (OP_CMD, OP_WRITE, OP_INDEXED, OP_BLOCK):
                raise SystemExit("refusing op %d without --write" % op)
            print(cam.call(op, c, val))

        else:
            print(__doc__)


if __name__ == "__main__":
    main()
