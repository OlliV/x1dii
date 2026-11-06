#!/usr/bin/env python3
"""Offline unpacker for Hasselblad X-System .cim firmware containers.

Container format (recovered from /system/bin/upgraded, X1D II 1.0.0 dump):

  0x000  public header, plaintext
           "VHABCIM\\r\\n" <hwid>_PVF\\r\\n <pvf>\\0\\r\\n <date>\\r\\n <time>\\r\\n
           \\x1a\\0\\0\\0\\0\\x03  41 88 6a 34  <u32>  ... zero pad ...
  0x080  encrypted payload (see below)

Encryption: AES-128-CBC **in 4096-byte chunks, each starting from a fixed IV**.
This is the crucial detail — a single continuous CBC chain decrypts every chunk's
first 16-byte block incorrectly, which corrupts exactly one block per 4 KiB and
breaks any compressed stream inside the payload.

    key = 93f82ca244ab29e0366934d56713eea2      (.rodata 0x4582c in `upgraded`)
    iv  = bd972536e78e1c3ebf385e01ae4adb52      (per-chunk restart)

Payload layout: private header (0x180) + 0x100-byte item records + item data.
Item record: +0x24 offset (BE), +0x28 length (BE), +0x2c 16-byte checksum,
             +0x60 name.  Recorded offsets are 0x80 higher than the real data.

Verified: the hbmanual PDF extracted this way is byte-identical to the copy the
camera exposes over USB mass storage.

Usage:
  cim_unpack.py info    <in.cim>
  cim_unpack.py decrypt <in.cim> <out.bin>
  cim_unpack.py extract <in.cim> <outdir>     # items + ota.zip entries + system.img
"""
import os
import struct
import sys
import zlib

KEY = bytes.fromhex("93f82ca244ab29e0366934d56713eea2")
IV = bytes.fromhex("bd972536e78e1c3ebf385e01ae4adb52")
BODY = 0x80
CHUNK = 4096


def public_header(data):
    raw = data[:BODY]
    parts = raw.split(b"\r\n")
    return {
        "magic": parts[0].decode("latin1"),
        "hwid": parts[1].decode("latin1") if len(parts) > 1 else "",
        "pvf": parts[2].rstrip(b"\0").decode("latin1") if len(parts) > 2 else "",
        "date": parts[3].decode("latin1") if len(parts) > 3 else "",
        "time": parts[4].decode("latin1") if len(parts) > 4 else "",
        "tag": raw[0x40:0x44].hex(),
    }


def _dec_chunks(body, start, length):
    """Decrypt length bytes of body starting at `start`, restarting the IV every
    4096 bytes measured from `start` (each item has its own chunk grid)."""
    from Crypto.Cipher import AES

    seg = body[start : start + length]
    seg = seg[: len(seg) // 16 * 16]
    out = bytearray()
    for off in range(0, len(seg), CHUNK):
        out += AES.new(KEY, AES.MODE_CBC, IV).decrypt(seg[off : off + CHUNK])
    return bytes(out)


def decrypt(data):
    """Decrypt the payload. Chunk grids are per item: the header region is its own
    grid anchored at the body start, and each item's grid is anchored at the item."""
    body = data[BODY:]
    body = body[: len(body) // 16 * 16]
    plain = bytearray(body)

    first_item = None
    for it in items(bytes(plain)):
        if first_item is None or it["offset"] < first_item:
            first_item = it["offset"]
    header_end = first_item if first_item else 0x580
    plain[:header_end] = _dec_chunks(body, 0, header_end)

    for it in items(bytes(plain)):
        start, length = it["offset"], it["length"]
        if start < 0 or length <= 0 or start + length > len(body):
            continue
        plain[start : start + length] = _dec_chunks(body, start, length)
    return bytes(plain)


def items(plain):
    out = []
    for rec in range(0x180, min(len(plain), 0x1000), 0x100):
        name = plain[rec + 0x60 : rec + 0x80].split(b"\0")[0].decode("latin1", "replace")
        if not name or not name.isprintable():
            continue
        off, ln = struct.unpack_from(">II", plain, rec + 0x24)
        out.append({"name": name, "offset": off - 0x80, "length": ln})
    return out


def zip_entries(plain, start, end):
    """Yield (name, data) for each entry of the embedded OTA zip."""
    o = plain.rfind(b"PK\x05\x06", start, end)
    if o < 0:
        return
    cd = o - struct.unpack_from("<I", plain, o + 12)[0]
    p = cd
    while plain[p : p + 4] == b"PK\x01\x02":
        f = struct.unpack_from("<IHHHHHHIIIHHHHHII", plain, p)
        name = plain[p + 46 : p + 46 + f[10]].decode("utf8", "replace")
        lo = start + f[16]
        if plain[lo : lo + 4] != b"PK\x03\x04":
            j = plain.find(b"PK\x03\x04", max(0, lo - 64), lo + 64)
            if j >= 0:
                lo = j
        if plain[lo : lo + 4] == b"PK\x03\x04":
            lh = struct.unpack_from("<IHHHHHIIIHH", plain, lo)
            st = lo + 30 + lh[9] + lh[10]
            raw = plain[st : st + f[8]]
            try:
                data = zlib.decompressobj(-15).decompress(raw) if f[4] == 8 else raw
            except zlib.error:
                data = b""
            yield name, data
        p += 46 + f[10] + f[11] + f[12]


def sdat_to_img(dat, transfer):
    """Convert Android sparse block data + transfer list into a raw image."""
    lines = [l.strip() for l in transfer.decode().split("\n") if l.strip()]
    img = bytearray()
    pos = 0
    for line in lines[3:]:
        head, _, args = line.partition(" ")
        nums = [int(x) for x in args.split(",")] if args else []
        if not nums:
            continue
        vals = nums[1 : 1 + nums[0]]
        pairs = list(zip(vals[0::2], vals[1::2]))
        end = max(b for _, b in pairs) * 4096
        if end > len(img):
            img.extend(b"\0" * (end - len(img)))
        if head == "new":
            for a, b in pairs:
                cnt = (b - a) * 4096
                img[a * 4096 : b * 4096] = dat[pos : pos + cnt]
                pos += cnt
    return bytes(img)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cmd, src = sys.argv[1], sys.argv[2]
    data = open(src, "rb").read()
    plain = decrypt(data)

    if cmd == "info":
        print(public_header(data))
        for it in items(plain):
            print(f"  {it['name']:20s} off={it['offset']:#x} len={it['length']:,}")
        return 0

    if cmd == "decrypt":
        open(sys.argv[3], "wb").write(plain)
        return 0

    if cmd == "extract":
        out = sys.argv[3]
        os.makedirs(out, exist_ok=True)
        ents = list(zip_entries(plain, 0x580, len(plain)))
        for name, blob in ents:
            path = os.path.join(out, name.replace("/", "_"))
            open(path, "wb").write(blob)
            print(f"  {name:48s} {len(blob):>12,d}")
        dat = os.path.join(out, "system.new.dat")
        tr = os.path.join(out, "system.transfer.list")
        if os.path.exists(dat) and os.path.exists(tr):
            img = sdat_to_img(open(dat, "rb").read(), open(tr, "rb").read())
            open(os.path.join(out, "system.img"), "wb").write(img)
            print(f"  system.img {len(img):,} bytes")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
