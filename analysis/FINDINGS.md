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

---

## Addendum — reversing the OTA `update-binary` (2026-11-04)

`update-binary` (607,224 bytes, identical size to the 1.0.0 copy) is a statically
linked ARM32 build of the Android 6 OTA updater:

| evidence | meaning |
|---|---|
| `bootable/recovery/minzip/Zip.c` strings | minzip is the zip reader |
| `minzip: Call to inflateInit2 failed` | raw inflate, `inflateInit2(-MAX_WBITS)` |
| `1.2.8` | zlib version |
| `IMGDIFF2`, `BSDIFF40`, `failed to read chunk %d deflate header data` | applypatch/imgdiff support is linked in |
| no `inflateSetDictionary` path in stock minzip | dictionary-compressed entries are *not* expected |

So the stock extraction path cannot use a preset dictionary — which weakens the
imgdiff/dictionary theory for these entries.

Two observations that matter more:

1. **Decryption is definitely correct.** `file_contexts` decrypts to valid
   SELinux policy text (`#line 1 "external/sepolicy/file_contexts" …`), and
   `metadata` / `MANIFEST.MF` / `CERT.SF` / `CERT.RSA` all verify by CRC-32.
2. **The central directory has been altered after the zip was built.** Entry
   `META-INF/com/google/android/updater-script` has 8 bytes of its name replaced
   by random data and its local-header offset field is garbage
   (`0x45c562fa`); every other entry's offset resolves to a real local header.

That combination — correct outer decryption, intact structural metadata, but
individual payload streams that fail at their first match while others decode
perfectly — points at selective scrambling of specific regions rather than a
whole-item cipher.

### Revised next steps

1. Reverse `upgraded`'s item handling (`Cim::FileReader`, `Cim::EncryptedFileReader`
   vs `UnencryptedFileReader`, 0x14894 / 0x13a00 / 0x193c4) — the container
   explicitly distinguishes encrypted from unencrypted *items*, so a per-item or
   per-region transform is the most likely missing step.
2. Diff a clean 1.0.0 OTA (public dump) against 1.5.2 to see whether entries were
   already wrapped in 1.0.0 — the 1.0.0 `system.new.dat`/`vendor.new.dat` are in
   the public dump and start with `09 00 00 00 0a 00 00 00 …`, not deflate.
3. Device route (ADB/FTP) remains the reliable bypass.

## Addendum 2 — payload streams are real deflate, but break mid-stream

Decoding the `update-binary` entry (data at `0x683`) produced concrete evidence:

* Its first 16 bytes are byte-identical to a **fresh `zlib.compressobj(6, -15)` of the
  1.0.0 `update-binary`** (`8cbc097c53c5f638…`). The entry is ordinary deflate.
* Feeding it with a 32 KiB pre-filled window decodes **143,024 bytes of real ARM
  code** that matches the 1.0.0 binary up to offset 316 — exactly where the two
  builds' BuildIDs differ (`c001c784…` vs `e1eb79a7…`). So the plaintext is right.
* Without a pre-filled window, zlib reports `invalid distance too far back` after
  10,923 bytes of output — the stream references history *before* its start.
* With a window, decoding stops at input byte 98,126 with `invalid literal/lengths
  set` — a structural (Huffman) break, independent of what the window contains.
* `system.new.dat` behaves the same way: 6,372,076 bytes of (mostly zero) output,
  then `invalid stored block lengths` at input 16,877.

Chunked-CBC with IV resets (11 candidate chunk sizes), 4/8/16-byte insertions at the
break, and per-entry preset dictionaries (zeros, 0xFF, private header, item table,
1.0.0 `update-binary`) were all tested and ruled out.

Interpretation: these entries were compressed with a **history/dictionary that is not
stored in the package** (imgdiff-style), and additionally the decodable prefix ends
at a hard structural break. Recovering them needs either the base image the packer
used, or a device-side `/system` copy.
