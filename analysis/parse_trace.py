#!/usr/bin/env python3
"""Decode the libusb trace captured from Phocus (see usbshim.c) into protocol frames.

Frame layouts recovered from the capture:

  host -> camera   (ep 0x04)
    04 00 08 05 <len8> <payload len8>
  camera -> host   (ep 0x85)
    03 00 05 08 <len8> <payload len8>

  payload = <seq u16 LE> <rest>

  request  rest = <op u8> [<code u16 LE>] [<value>]
  reply    rest = <status u8> [<data>]

Status byte high bit set = command accepted; 0x80|0x40 = in-progress/split.
The low nibble looks like a value type: 1=int32, 2=string, 5=u16 array, 6=tagged.
"""
import re
import sys

REQ_MAGIC = "04 00 08 05"
REP_MAGIC = "03 00 05 08"

OPS = {
    1: "WRITE",
    2: "READ",
    3: "WRITE_STR",
    6: "READ_MULTI",
    4: "CMD4",
}

STATUS_TYPE = {
    0x81: "int32",
    0x82: "string",
    0x83: "u32?",
    0x84: "u32",
    0x85: "u16[]",
    0x86: "tagged",
    0x40: "in-progress",
}


def load_codes(path=None):
    """Parse the parameter table dump into {code: name}."""
    import os
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parameter-table.txt")
    codes = {}
    try:
        for line in open(path):
            m = re.match(r"\s*(0x[0-9a-fA-F]{4})\s+(\S+)", line)
            if m:
                codes[int(m.group(1), 16)] = m.group(2)
    except OSError:
        pass
    return codes


CODES = load_codes()


def code_name(code):
    return CODES.get(code, "")


def asc(bs):
    out = []
    for b in bs:
        out.append(chr(b) if 32 <= b < 127 else ".")
    return "".join(out)


def parse(text):
    return bytes(int(x, 16) for x in text.split())


def split_frames(entry):
    """Split a hexdump into frames; a single bulk read may carry several."""
    frames = []
    i = 0
    while i + 5 <= len(entry):
        if entry[i] in (0x03, 0x04) and entry[i + 1] == 0x00:
            n = 5 + entry[i + 4]
            if n <= len(entry) - i:
                frames.append(entry[i:i + n])
                i += n
                continue
        break
    return frames


def decode_value(tag, data):
    if tag == 0x81 and len(data) >= 4:
        v = int.from_bytes(data[:4], "little", signed=True)
        return "int32 %d (0x%08x)" % (v, v & 0xFFFFFFFF)
    if tag == 0x82:
        return "str %r" % data.decode("latin-1")
    if tag == 0x83 and len(data) >= 4:
        return "u32 %d" % int.from_bytes(data[:4], "little")
    if tag == 0x84 and len(data) >= 4:
        return "u32 %d" % int.from_bytes(data[:4], "little")
    if tag == 0x85:
        vals = [int.from_bytes(data[i:i + 2], "little")
                for i in range(0, len(data) - 1, 2)]
        names = [code_name(v) for v in vals]
        if any(names):
            return "codes %s" % ", ".join(
                "%s(%s)" % (n, hex(v)) if n else hex(v)
                for v, n in zip(vals, names))
        return "u16[] " + " ".join("%04x" % v for v in vals)
    if tag == 0x86:
        return "tagged %s (%r)" % (data.hex(" "), asc(data))
    return data.hex(" ")


def decode_request(f):
    payload = f[5:]
    seq = int.from_bytes(payload[:2], "little")
    rest = payload[2:]
    op = rest[0] if rest else None
    code = None
    value = b""
    if len(rest) >= 3:
        code = int.from_bytes(rest[1:3], "little")
        value = rest[3:]
    return seq, op, code, value


def decode_reply(f):
    payload = f[5:]
    seq = int.from_bytes(payload[:2], "little")
    rest = payload[2:]
    status = rest[0] if rest else None
    return seq, status, rest[1:]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/libusb_trace.log"
    pending = {}
    out = []
    cur_ep = 0
    for line in open(path, errors="replace"):
        line = line.rstrip("\n")
        m = re.match(r"([\d.]+)\s+(SUBMIT|COMPLETE)\s+(IN |OUT)\s+(\w+)\s+ep=0x(\w+)\s+len=(\d+)", line)
        m2 = re.match(r"[\d.]+\s+(<-\s*data:|-> data:)\s*(.*)", line)
        if m:
            cur_ep = int(m.group(5), 16)
            continue
        if m2:
            if cur_ep not in (0x04, 0x85):
                continue
            raw = parse(m2.group(2))
            for f in split_frames(raw):
                if f[0] == 0x04:
                    seq, op, code, value = decode_request(f)
                    pending[seq] = (op, code, value)
                    out.append(("REQ", seq, op, code, value, None, None))
                else:
                    seq, status, data = decode_reply(f)
                    out.append(("REP", seq, None, None, None, status, data))
    # pair replies with requests
    reqs = {}
    for row in out:
        if row[0] == "REQ":
            reqs[row[1]] = row
    print("%-6s %-6s %-10s %-6s %-28s %s" % ("seq", "dir", "op", "code", "name", "value"))
    for kind, seq, op, code, value, status, data in out:
        if kind == "REQ":
            nm = code_name(code) if code is not None else ""
            vs = asc(value) if value and all(32 <= b < 127 for b in value) else value.hex(" ")
            print("%-6d %-6s %-10s 0x%04x %-28s %s" % (
                seq, "->", OPS.get(op, op), code if code is not None else 0, nm, vs))
        else:
            t = STATUS_TYPE.get(status, "%02x" % status) if status is not None else "?"
            req = reqs.get(seq)
            tag = req[2] if req else -1
            print("%-6d %-6s %-10s %-6s %-28s %s" % (
                seq, "<-", t, "", "", decode_value(status, data) if status is not None else ""))


if __name__ == "__main__":
    main()
