# X1D II 1.5.2 CIM — offline unpack attempt (2026-11-04)

Status: **container decrypted; system image payload still locked.**

## 1. Key recovery

The camera's own upgrader at `/system/bin/upgraded` (ARM32, taken from the public
v1.0.0 dump) links `AES_set_decrypt_key` and `AES_cbc_encrypt`. The only caller
chain is:

| Address | Role |
|---|---|
| `0x14f40` | Cim::decryptItem — `AES_set_decrypt_key(key,128,&sched)` then `AES_cbc_encrypt(in,out,len,&sched,iv,0)` |
| `0x14ea4` | virtual wrapper; passes `this+4` as key and `this+0x24` as IV |
| `0x4582c` (.rodata) | the 16-byte key |

Brute-forcing every 16-byte window of `upgraded` against the 1.5.2 body found
exactly one key that produces structured plaintext:

```
AES-128 key: 93f82ca244ab29e0366934d56713eea2
```

Mode is CBC over the whole body starting at file offset `0x80`. The true IV is
not yet recovered, so the first 16 plaintext bytes are unreliable; everything
after those decrypts into coherent container data.

## 2. Container layout (verified against 1.5.2)

```
0x000  public header (plaintext)
       "VHABCIM\r\n" "1700000_PVF\r\n" "10.00.29.07\0\r\n" "2022-08-26\r\n" "17:41:09\r\n"
       \x1a\0\0\0\0\x03  41 88 6a 34  <per-file u32>  ...zero pad...
0x080  AES-128-CBC payload:
         private header            0x180 bytes
         item records              4 x 0x100 bytes
         item payloads             concatenated
```

Item record (big-endian fields, name at `+0x60`):

| +offset | field |
|---|---|
| 0x00 | 16-byte checksum |
| 0x24 | u32 offset |
| 0x28 | u32 length |
| 0x2c | 16-byte checksum |
| 0x60 | image name (NUL-padded, `\r\n` terminated) |

Recorded offsets are **0x80 bytes higher** than the real data start (the packer
counts from after the public header). This is why each payload's first 0x80
bytes look overwritten.

Items in 1.5.2:

| name | offset | length |
|---|---|---|
| `ota.zip` | 0x600 | 158,063,021 |
| `hbl-upgrade` | 0x96be000 | 4,271 |
| `hbl-post-upgrade` | 0x96bf200 | 1,344 |
| `hbmanual_pack.tar` | 0x96bf800 | 75,806,720 |

## 3. What was extracted

* `analysis/extracted/hbl-upgrade.sh` — the A/B slot upgrade driver (`unrd`
  slot_1/slot_2 switching, `dji-spinor` module load, CPLD recovery path).
* `analysis/extracted/hbl-post-upgrade.sh` — post-upgrade verification and boot
  selection.
* `hbmanual_pack.tar` — 75 MB of 907X / 907XAE80 quick-start PDFs, not firmware.
* The embedded `ota.zip`, 9 of 17 entries recovered and CRC-verified
  (metadata, `file_contexts`, transfer lists, `MANIFEST.MF`, `CERT.SF`,
  `CERT.RSA`).

## 4. The blocker

The system payloads (`system.new.dat` 243.7 MB, `vendor.new.dat` 17.5 MB,
`recovery.img` 23.3 MB, `normal.img` 7.7 MB, `bootarea.img` 1.4 MB,
`update-binary`) are raw-deflate streams that reference data outside their own
stream:

```
zlib error: invalid distance too far back   (update-binary, immediately)
zlib error: invalid stored block lengths    (system.new.dat, after 6.3 MB out)
```

That is the signature of deflate with a **preset dictionary** — the same
technique AOSP's `imgdiff` uses. The dictionary is not stored in the package.

Dictionaries tried and rejected: 32 KiB of zeros, 0xFF, the private header, the
item table, the 1.0.0 `update-binary`. The local `.cim` is byte-identical to
Hasselblad's CDN copy (`sha256 1bb69d26…`), so this is packaging, not a bad
download.

## 5. Next steps

1. Reverse `Cim::ItemHeader` parse (`0x193c4`, `0x16934`) and
   `Cim::FileReader` (`0x14894`, `0x13a00`) — the camera must obtain the
   dictionary from somewhere, most likely the item header or the base partition.
2. Check whether `upgraded` hands the zip to `update_engine` untouched (it
   imports no zlib, so decompression happens elsewhere) — the recovery
   `update-binary` is the actual inflater.
3. Alternative: obtain `/system` from a device (`adb` in non-production mode,
   or the DJI FTP channel) — bypasses the container question entirely.
