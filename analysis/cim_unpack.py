#!/usr/bin/env python3
"""Offline unpacker for Hasselblad X1D II .cim firmware containers.

Recovered from /system/bin/upgraded (X1D II 1.0.0 dump, ARM32):
  func 0x14f40 -> AES_set_decrypt_key(key, 0x80, &sched) + AES_cbc_encrypt(..., enc=0)
  key at .rodata 0x4582c, 16 bytes.

Container layout (offsets are file offsets):
  0x000  public header, plaintext:
           "VHABCIM\r\n" <hwid>_PVF\r\n <pvf-version>\0\r\n <date>\r\n <time>\r\n
           then 1a 00 00 00 00 03, 41 88 6a 34, a per-file u32, zero padding to 0x80
  0x080  AES-128-CBC payload (single continuous stream) =>
           private header (0x180 bytes) + item table (0x100-byte records)
           + item payloads
  Item record fields observed (big-endian u32):
           +0x24 offset, +0x28 length, +0x2c 16-byte checksum, +0x60 image name
  NOTE: recorded offsets appear 0x80 bytes higher than the real data start.

Usage:
  cim_unpack.py decrypt  <in.cim> <out.bin>     # raw decrypted payload
  cim_unpack.py info     <in.cim>               # header + item table
  cim_unpack.py carve    <in.cim> <outdir>      # dump plaintext items
"""
import struct, sys

KEY = bytes.fromhex("93f82ca244ab29e0366934d56713eea2")
BODY = 0x80


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


def decrypt(data):
    """AES-128-CBC with the recovered key. The true IV is still unknown, so the
    first 16 plaintext bytes are unreliable; everything else verifies as valid
    deflate/zip/tar content."""
    from Crypto.Cipher import AES

    body = data[BODY:]
    body = body[: len(body) // 16 * 16]
    return AES.new(KEY, AES.MODE_CBC, b"\0" * 16).decrypt(body)


def items(plain):
    """Best-effort item table scan: 0x100-byte records carrying a name at +0x60."""
    out = []
    for rec in range(0x180, min(len(plain), 0x1000), 0x100):
        name = plain[rec + 0x60 : rec + 0x80].split(b"\0")[0].decode("latin1", "replace")
        if not name or not name.isprintable():
            continue
        off, ln = struct.unpack_from(">II", plain, rec + 0x24)
        out.append({"name": name, "offset": off, "length": ln,
                    "checksum": plain[rec : rec + 16].hex()})
    return out


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cmd, src = sys.argv[1], sys.argv[2]
    data = open(src, "rb").read()
    if cmd == "decrypt":
        open(sys.argv[3], "wb").write(decrypt(data))
        return 0
    if cmd == "info":
        print(public_header(data))
        plain = decrypt(data)
        for it in items(plain):
            print(f"  {it['name']:20s} off={it['offset']:#x} len={it['length']:,}"
                  f" checksum={it['checksum']}")
        return 0
    if cmd == "carve":
        import os
        plain = decrypt(data)
        os.makedirs(sys.argv[3], exist_ok=True)
        for it in items(plain):
            start = it["offset"] - 0x80  # recorded offset is 0x80 high
            if start < 0 or it["length"] <= 0 or start + it["length"] > len(plain):
                continue
            path = os.path.join(sys.argv[3], it["name"].replace("/", "_"))
            open(path, "wb").write(plain[start : start + it["length"]])
            print("wrote", path)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
