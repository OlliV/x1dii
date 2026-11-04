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

---

## Addendum 3 — X1D II GUI architecture (from the 1.0.0 dump)

`system.img/bin/victory-gui-static` (20.6 MB, ARM32, stripped):

| fact | evidence |
|---|---|
| Qt 5.11.1, **static** build | `Qt 5.11.1 (arm-little_endian-ilp32-eabi static release build…)` |
| dynamically linked only to platform libs | `libduml_util/frwk/hal`, `libwayland-client`, `libxkbcommon`, `libz`, `libc++/c` |
| QML is **compiled to C++** (Qt Quick Compiler) | no `.qml` sources, no `qv4jsc`/qmlcache magic, no zlib-compressed QML resources |
| only QML *names* survive | `SettingsGeneric.qml`, `AutoFocus.qml`, `EVFWindow.qml`, `displaytest.qml`, `keytest.qml` as registration strings |
| C++ popover framework | `PopoverDriveMode`, `PopoverExposureMode`, `PopoverFocusMode`, `PopoverMeterMethod`, `PopoverWhiteBalance`, `PopoverError` |
| capability-gated features | `CapabilityFlag` enum incl. `CapabilityVideoMode`, `CapabilityContinuousDrive`, `CapabilityHasBmode/Tmode` |
| video plumbing exists | `VideoProxyUI`, `toggle_video_recording()`, `E_CameraMode_Video`, `E_DialMode_Video`, video icons |

Consequence: the X2D route (drop QML units on disk + flip a menu-cache pointer) does **not**
port. X1D II has no QML on disk; the UI is ARM code. Adding a page/entry means either
patching `victory-gui-static` or hooking it at runtime (LD_PRELOAD is viable — the
binary is dynamically linked to libc and the duml libs).

Non-UI features (startup logo, service trimming, governor, boot scripts) remain
script/config work in `/system` and need no GUI patch.

---

## Addendum 4 — sensor stream profile and video capability (1.0.0 image)

`system.img/etc/imx161f_ec1704.sp` (24 KB JSON, plaintext) is the whole pipeline
description. Profile `imx161_with_fpga`, seven stream groups:

| group | streams |
|---|---|
| `still_r43_cap` | liveview 1280×960 / 1024×768 (LCD) / 640×480 (CFV) / 960×720 / 1440×1080 (HDMI), RGBA8888; RAW16 2756×1240, 2816×1260, 1280×576; still 4136×3100 YUV8_SP422; still_raw 8384×6304 RAW16 |
| `still_r43_jandr_cap`, `_jpeg_cap` | still_yuv 8256×6200 YUV8_SP422, raw pipeline, `plat_imgtec_rst` JPEG encoder |
| `still_r43_raw_playback`, `_normal_raw_pattern` | raw playback / raw pattern |
| `video_1080P@2997` | yuv_piv 2756×1552 YUV8_SP420 (dzoom filter) → video 1920×1080 |
| `video_2720P@2997` | same source → video 2720×1530 |

Every video stream is `DUSS_PIXFMT_YUV8_SP420_YUV` — **8-bit, 4:2:0**. There is no
10-bit pixel format anywhere in the video path. The encoder can do 10-bit
(`422SP10`, `hlg10_enable`), but the ISP/FPGA never produces it.

### ProRes

* kernel module `proresenc_mod.ko`, `dji,eagle-proresenc` driver
* userspace `proresenc` (30 KB): `-w -h -p profile -T type`, profiles 0–5 =
  422HQ / 422 / 422LT / 422Proxy / 4444 / 4444XQ; pixel formats 422SP8, 422SP10,
  444P8, 444P10; types 4K / SPLICE8K / 8K
* `libproresenc.so`, `lib/modules/proresenc_mod.ko`

References to ProRes in `camera`, `camservice`, `victory-gui-static`: **zero**.
The stack is present but entirely unwired.

### Video resolutions available

The only video source is 2756×1552. Anything wider (4K 3840) is impossible without
a new sensor readout mode; the still sensor can do 8384×6304 but not at 29.97 fps.

---

## Addendum 5 — how encoding is actually wired

`/system/etc/dji.json` (119 KB, runtime config) contains a HAL device table:

```json
{"name": "/dev/proresenc", "attach": "duss_hal_attach_dji_plenc",  "detach": "..."},
{"name": "/dev/venc0",     "attach": "duss_hal_attach_imgtec_venc", "detach": "..."},
{"name": "/dev/venc1",     "attach": "duss_hal_attach_imgtec_venc", "detach": "..."}
```

So the ProRes encoder device is **registered and attachable at runtime**, alongside the
two imgtec H.264 encoders.

| layer | evidence |
|---|---|
| `libduml_frwk.so` | `wl_codec_venc.c`, `duss_wl_channel_create_codec`, `codec_rate_ctrl:set %d bps` — the video encoder channel |
| `libduml_hal.so` | `duss_hal_attach_imgtec_venc`, `duss_hal_venc_encfrm`, **plus** `ProresEnc_Init/EncodeFrame/Deinit/GetErrTips` and codec-name strings `prores`, `prores_splice8k`, `prores_8k`, `unsupported codec %s!` |
| `libduml_media.so` | `duss_hal_device_open(venc0)`, `duss_hal_device_start(h264)` — H.264 named explicitly |
| `bin/test_hal_plenc` | factory test for the ProRes encoder HAL |

`etc/venc_test/*.json` are fixtures for `test_imgtec_venc`, not runtime configuration.

### Codec verdicts

* **HEVC** — hardware can do it (`libomx_vxd.so`), but no `h265`/`hevc` string exists in the
  framework or HAL; the wrapper starts `h264` by name. Would need code changes.
* **ProRes** — HAL device registered in `dji.json`, library linked, API present, factory test
  shipped. Not used by the video path. Missing: 4:2:2 frames (ProRes takes 422SP8/422SP10/
  444P8/444P10; the video pipe is 420) and MOV muxing (`libduml_media` has libavformat).
  The ISP can already emit `YUV8_SP422` — the still path does.

---

## Addendum 6 — `test_hal_plenc`: the working ProRes call sequence

Imports (2.3 KB of Thumb-2 code, stripped) and their call sites give the full contract:

| step | API |
|---|---|
| 1 | `duss_log_initialize`, `duss_hal_initialize` |
| 2 | `duss_hal_device_open` (plenc device) |
| 3 | `duss_hal_device_start(handle, 0)` |
| 4 | `duss_hal_mem_alloc` + `duss_hal_mem_map` ×2 (frame buffer, stream buffer) |
| 5 | `duss_hal_attach_ion_mem` (import guest memory) |
| 6 | loop: `duss_hal_plenc_encfrm(...)` per frame |
| 7 | `duss_hal_mem_free`, `duss_hal_device_stop`, `duss_hal_device_close`, `duss_hal_deinitialize` |

CLI contract:

```
test_hal_plenc -i inFile -s statFile -w width -h height [-o outFile]
               -p profile(0-5)  -t 422SP8|422SP10|444P8|444P10
               -c color  -T type(0-2)  -F fps
```

* input is a **raw YUV file**, width/height must be 8-aligned
* pixel formats accepted: **422SP8, 422SP10, 444P8, 444P10** — 422 semi-planar or
  444 planar only. There is **no 4:2:0** input path.
* profiles 0-5 = 422HQ / 422 / 422LT / 422Proxy / 4444 / 4444XQ

### What this means for the camera

The video pipe emits `YUV8_SP420_YUV`. To feed the ProRes encoder it must become
`422SP8` (or 10-bit `422SP10`). The ISP already produces `YUV8_SP422` for the still
path, so a 4:2:2 node at video rates is plausible; otherwise a chroma upsample step
is needed. Muxing is covered: `libduml_media` carries libavformat.

Bandwidth check at 2720×1530: 422SP8 ≈ 8.3 MB/frame ≈ 250 MB/s at 29.97 fps;
ProRes 422HQ ≈ 440 Mbps ≈ 55 MB/s to the card — both comfortable.

---

## Addendum 7 — `duss_hal_plenc_encfrm` contract (libduml_hal.so)

`libduml_hal.so` keeps its symbols; the ProRes path is `hardware/dji/duml/hal/codec/plenc/dji_plenc.c`.

Export table:

| symbol | addr | size |
|---|---|---|
| `duss_hal_plenc_encfrm` | 0xbd4c | 72 |
| `duss_hal_attach_dji_plenc` | 0x231b4 | 204 |
| `duss_hal_device_open` | 0xb614 | 528 |
| `duss_hal_mem_alloc` | 0xaab0 | 96 |

`duss_hal_plenc_encfrm(handle, frame)` is a thin wrapper:

```
ctx = handle->0x2c            // private ops, set by attach
if (!ctx->0) return -0x3f1
lock(handle->0xc)
if ((handle->flags & 2) == 0) return err   // must be started
ret = ctx->0(handle, frame)                // real encoder
unlock
```

The real encoder (`0x22f1c`) checks the frame descriptor:

```
w = ctx->0x14c ; h = ctx->0x150
if (frame->size != w*h) -> "stat buf length ... isn't correct"
```

so the descriptor is at least:

| offset | meaning |
|---|---|
| +0x00 | pointer to buffer struct |
| +0x04 | offset within buffer |
| +0x08 | size in pixels (must equal width×height) |

and the buffer struct carries the DMA handle at +0x0c and plane addresses at
+0x28 / +0x2c / +0x3c / +0x4c (Y / Cb / Cr, matching 422 semi-planar or 444 planar).

Inside, the HAL calls `ProresEnc_Init` / `ProresEnc_EncodeFrame` from
`libproresenc.so` and needs:

* frame buffer — physical address (`get base phy addr of frame buffer fail!`)
* stream buffer — physical + virtual address
* stat buffer — length must be width×height (`stat buf length(%d) isn't correct, should be %d x %d!`)

### Status

The call contract is now known end to end. Remaining gap is configuration: where
width/height/profile/pixel-format are handed to `ProresEnc_Init` (likely the 0x15c-byte
struct passed through `duss_hal_attach_dji_plenc`), and the 4:2:0 → 4:2:2 conversion
for the video pipe's `YUV8_SP420_YUV` frames.

---

## Addendum 8 — CORRECTION: the payload is chunked CBC, not one chain

Every earlier "corruption", "tampered CD entry", "dictionary-compressed deflate" and
"structural break" in this document was an artifact of decrypting the body as a single
CBC chain. The container restarts the IV every 4096 bytes.

```
key = 93f82ca244ab29e0366934d56713eea2
iv  = bd972536e78e1c3ebf385e01ae4adb52      (fixed, restarted every 4096 bytes)
```

Chunk grids are **per item**: the header region is anchored at the body start, and each
item's grid is anchored at that item's first byte. A global grid corrupts the first
16-byte block of every chunk — one block per 4 KiB — which is exactly what broke the
deflate streams.

How it was found: the camera was connected over USB and exposes the `HBMANUAL`
partition. Comparing our extracted manual PDF with the camera's own copy showed 846
differing runs, all 16 bytes and all 4096 bytes apart. Solving
`IV = P_true XOR P_wrong XOR C_prev` gave the same value for every run.

Verified: the PDF extracted from the CIM is **byte-identical** to the file on the
camera (`sha256 ffc59b66…`).

With the corrected scheme:

* all **17/17** OTA entries extract with valid CRC-32
* `system.new.dat` (243,724,288 B) → `system.img` (256 MiB ext4) builds cleanly
* `analysis/cim_unpack.py` reproduces it (`extract` mode)

Superseded claims: `§Addendum 2` (dictionary-compressed deflate), the "tampered
central-directory entry" in `§Addendum 1`, and the unreadable-first-block note in §1.

### What 1.5.2 actually contains

| item | finding |
|---|---|
| video groups | `video_1080P@2997`, `video_2720P@2997`, **`video_1240P@2997` (2756×1240)**, **`video_2064P@2997` (2756×2064)** |
| pixel formats | `YUV8_SP420_YUV` (video), `YUV8_SP422_YUV` (stills), `RGBA8888`, `RAW16` — **no 10-bit format anywhere** |
| 10-bit | absent in 1.5.2, confirmed at the pipeline-config level |
| ProRes | `libduml_hal.so` 14 refs, `dji.json` registers `/dev/proresenc`, GUI 0 refs — still unwired |
| GUI | 22,259,956 B (1.0.0: 20,617,428), has `2.7K, 2720x1530, 29.97 fps` and video UI strings |

---

## Addendum 9 — USB composite of a live X1D II (firmware 1.5.2)

Enumerated over USB (VID `0x2756`, PID `0x0007`, serial `VQ20200944`, bcdDevice 4.04):

| # | class | endpoints | what |
|---|---|---|---|
| 0 | `e0/01/03` | `0x82` int | RNDIS control |
| 1 | `0a/00/00` | `0x81`, `0x01` bulk | RNDIS data |
| 2 | `08/06/50` | `0x83`, `0x02` bulk | mass storage (SD ×2 + HBMANUAL) |
| 3 | `ff/00/00` | `0x03`,`0x04` OUT · `0x84`,`0x85` IN | **vendor bulk = DUML channel** |
| 4 | `02/02/01` | `0x87` int | CDC ACM control |
| 5 | `0a/00/00` | `0x86`, `0x05` bulk | CDC ACM data |

Interface 3 claims cleanly with libusb and is **silent** until the host initiates — no
unsolicited traffic on `0x84`/`0x85`.

DUML framing lives in `libduml_frwk.so`; the packers (crc8_append + crc16_append) are at
`0x22196`, `0x3ec34`, `0x49e9a`, with verify counterparts at `0x2220e`, `0x400f6`,
`0x49c6c`. Deriving the header layout from those is the next step before any frame is sent.

macOS note: RNDIS (interfaces 0/1) does not bind — no network route from this host, so
`dji_ftpd` (TCP 21) is unreachable over USB. The vendor bulk channel is the only path.

---

## Addendum 10 — USB channel probe and what Phocus reveals

Probing the vendor bulk interface (intf 3) with libusb on macOS:

| action | result |
|---|---|
| claim interface 3 | succeeds when Phocus is not running, `Errno 13` when it is |
| read `0x84` / `0x85` idle | silent, no unsolicited traffic |
| write `0x03` | times out (no reader behind that endpoint) |
| write `0x04` | accepted |
| crafted heartbeat / device-type frames | accepted, **no response** |

Frames were built from `CFiProtocol`'s own assembler (CRC8 table extracted and byte-identical
to poly 0x8C; CRC16 seed 0x3692), so the encoding matches — the silence is a state or
semantics gap, not a checksum error.

Phocus's own stderr log (running the app from a terminal) is more informative than the
frames would be for API shape:

```
Device Arrived USB 0x2756 0x7
CCameraDeviceUSBx::WriteParameter(Device:TetheredMode) returned 80c80001
CameraUSB: Timeout waiting for response!
CameraUSB: Action(FocusPositionUpdate) failed 0x80d60008
ReadParameter(Device:FaceDetection) failed 80d60006
Image Appeared id: 7735 ... 3FR
ReadData((4 7735) 0 8388608) took 0.099 s
Action(UnlockCamera) failed 0x80c80001
```

So the API is **name-addressed**: `WriteParameter(name)`, `ReadParameter(name)`,
`Action(name)`, `ReadData(id, offset, len)`. Tethering is a parameter write, not a mode
switch — and it reports "not supported" while everything else works, which is why
re-toggling tether mode had no effect on the bulk channel.

Still missing: the byte encoding of a request (payload layout for a named parameter).

Next: capture on a TCP transport. The camera's Wi-Fi exposes 80/443 (Phocus HTTP) when
reachable; desktop Phocus does not use Wi-Fi, so the capture would need Phocus Mobile.
Otherwise, reverse `CCameraDeviceUSBx::ReadParameter` / `WriteParameter` directly.
