# ProRes on the X1D II 50C — design note

Status: research note, no camera modified. Everything below is from offline analysis of
firmware **1.0.0** (public dump) and **1.5.2** (encrypted CIM, container decrypted).
Confidence is marked where it matters.

---

## 1. Summary

The X1D II ships H.264 video at 2720×1530 and 1920×1080, 29.97 fps, 4:2:0 8-bit
(added in firmware 1.2.0). The same firmware image contains a **complete, unused ProRes
encoder stack**: kernel driver, userspace library, CLI tool, HAL device registration, and
a factory test binary. Nothing in `camera`, `camservice`, or the GUI references it.

Enabling ProRes is therefore an **integration** problem, not a hardware-bring-up problem.
The encoder contract is now reverse-engineered end to end. Two gaps remain: the
configuration struct handed to `ProresEnc_Init`, and a 4:2:0 → 4:2:2 conversion in the
video pipeline (the ProRes encoder accepts 4:2:2 or 4:4:4 only).

Estimated shape: **1 offline session** to close the config gap, then a device-side proof
of concept, then pipeline work.

---

## 2. What the camera ships today

| Item | Value | Source |
|---|---|---|
| Video modes | 2720×1530, 1920×1080 @ 29.97 (1.2.0+) plus 2756×1240 and 2756×2064 in 1.5.2 | fw notes, `.sp` profiles |
| Codec | H.264 | `duss_hal_device_start(h264)` |
| Colour | 4:2:0 8-bit | fw 1.2.0 notes, `DUSS_PIXFMT_YUV8_SP420_YUV` |
| Stream profile | `video_1080P@2997`, `video_2720P@2997` | `/system/etc/imx161f_ec1704.sp` |

Both modes scale the same source: `yuv_piv` at **2756×1552**, 29.97 fps, through a `dzoom`
filter. There is no readout wider than 2756, so **4K is not achievable** without new
sensor/FPGA modes.

---

## 3. Platform capability inventory

| Capability | Evidence | Used? |
|---|---|---|
| H.264 encode | `/dev/venc0`, `/dev/venc1` → `duss_hal_attach_imgtec_venc` | ✅ |
| HEVC encode/decode | HEVC paths in `libomx_vxd.so` | ❌ (no `h265`/`hevc` string in HAL or framework) |
| 10-bit pixel formats | VXD format strings; `hlg10_enable` / `bitdepth_opt` in venc fixtures | ❌ confirmed absent in the 1.5.2 `.sp` (YUV8 only) |
| 4K30 encode | `etc/venc_test/venc_4kUHD_test.json` (3840×2160 @ 150 Mbps) | ❌ (no 4K source) |
| **ProRes** | see §4 | ❌ |

### ProRes components present

| File | Role |
|---|---|
| `lib/modules/proresenc_mod.ko` | driver, `of:N*T*Cdji,eagle-proresenc*`, ioctls `MREQ`/`MREL`/`WAIT` |
| `lib/libproresenc.so` | `ProresEnc_Init` / `EncodeFrame` / `Deinit` / `GetErrTips` |
| `bin/proresenc` | CLI: `-w -h -p profile -T type`, profiles 0–5 |
| `bin/test_hal_plenc` | factory test, full device open → encode → close sequence |
| `lib/libduml_hal.so` | HAL glue, source path `hardware/dji/duml/hal/codec/plenc/dji_plenc.c` |
| `etc/dji.json` | registers `/dev/proresenc` with `duss_hal_attach_dji_plenc` |

Profiles: 0–5 = 422HQ, 422, 422LT, 422Proxy, 4444, 4444XQ.
Input formats: `422SP8`, `422SP10`, `444P8`, `444P10` — **no 4:2:0**.

---

## 4. The encoder contract (reverse-engineered)

### 4.1 Public entry point

```c
int duss_hal_plenc_encfrm(handle, frame);   // handle from duss_hal_device_open
```

`duss_hal_plenc_encfrm` (`libduml_hal.so` @ `0xbd4c`, 72 B) is a wrapper:

```
ctx = handle->0x2c
if (!ctx->0) return -0x3f1
lock(handle->0xc)
if ((handle->flags & 2) == 0) return err        // must be started
ret = ctx->0(handle, frame)                     // real encoder @ 0x22f1c
unlock
```

### 4.2 Frame descriptor

| Offset | Meaning |
|---|---|
| `+0x00` | pointer to buffer struct |
| `+0x04` | offset within buffer |
| `+0x08` | size in pixels; **must equal width × height** |

Buffer struct: DMA handle at `+0x0c`, stride at `+0x28`, plane addresses at
`+0x2c` / `+0x3c` / `+0x4c`.

### 4.3 Requirements inside the HAL

* frame buffer must have a **physical address**
  (`get base phy addr of frame buffer fail!`)
* stream buffer needs **physical and virtual** addresses
* stat buffer length must be `width × height`
  (`stat buf length(%d) isn't correct, should be %d x %d!`)

### 4.4 Call sequence (from `test_hal_plenc`)

```
duss_log_initialize / duss_hal_initialize
duss_hal_device_open("/dev/proresenc")
duss_hal_device_start(handle, 0)
duss_hal_mem_alloc + duss_hal_mem_map      ×2   (frame, stream)
duss_hal_attach_ion_mem                         (import camera buffers)
loop: duss_hal_plenc_encfrm(handle, &frame)
duss_hal_mem_free / device_stop / device_close / duss_hal_deinitialize
```

### 4.5 Bandwidth sanity

At 2720×1530: 422SP8 ≈ 8.3 MB/frame ≈ **250 MB/s** at 29.97 fps; ProRes 422HQ output
≈ 440 Mbps ≈ **55 MB/s** to card. Both are comfortable for UHS-II and the DDR budget.

---

## 5. Integration options

**A. ISP emits 4:2:2 at video rates.** The still path already produces
`YUV8_SP422` at 8256×6200, so the hardware can. If the video readout can be configured
for 422, this is a config change in the `.sp` profile plus encoder config. *Preferred.*

**B. Chroma-upsample node in front of the encoder.** 420 → 422 conversion as a DUSS
filter between `video` stream and `plenc`. Computationally cheap; adds a pipeline node
and a buffer copy (or a shader-less scaling filter).

**C. Feed the encoder from a still-rate capture path.** Not viable for 29.97 fps.

Option A should be investigated first because it avoids new pipeline code entirely.

---

## 6. Gaps and open questions

1. **Configuration struct** — partially mapped (see §6.1). `ProresEnc_Init` is called as
   `(cfg, stat, &handle)` from `libduml_hal.so` `0x22e2c`; the remaining unknown is which
   upstream field supplies width and height.
2. **Rate control** — what `stat` buffer contents the encoder expects (all-zero is what
   `test_hal_plenc` uses with a stat *file*; the CLI warns "if using default stat, rate
   control will be very bad").
3. **422 at video rate** — unknown until the 1.5.2 `.sp` and `farmus` are readable, or
   until tested on a device.
4. **Muxing** — `libduml_media` carries libavformat, but the MOV/ProRes muxer path is
   unproven on this build.
5. **UI** — the 1.5.2 system image is now extractable (see `FINDINGS.md` addendum 8), and
   the GUI is *still* compiled (Qt Quick Compiler), so exposing ProRes in the menu needs a
   binary patch or a runtime hook regardless.
6. **Is `/dev/proresenc` present in 1.5.2?** Registered in `dji.json` in 1.0.0; verify on
   device (`ls /dev | grep prores`, `grep prores /system/etc/dji.json`).

### 6.1 Config struct (progress)

`ProresEnc_Init(cfg, stat, &handle)` is called from `libduml_hal.so` `0x22e2c`:

```
r0 = ctx + 0x100     // config struct
r1 = ctx + 0x13c     // stat buffer
r2 = ctx + 0x158     // encoder handle out
```

Config struct layout (offsets from `ctx+0x100`), filled from a device config struct `src`:

| Offset | Source | Meaning (working hypothesis) |
|---|---|---|
| `+0x00` | string compare on `src[0]` | profile selector, 0/1/2 |
| `+0x04` | `src+0x14` | dimension A |
| `+0x08` | `src+0x18` | dimension B |
| `+0x0c` | `src+0x08` | plane/stride or bitrate |
| `+0x10` | `src+0x04`, mapped | pixel format: `0x67`→0, `0x44f`→1, `0xca`→2, `0x4b2`→3 |
| `+0x14` | `src+0x0c` | |
| `+0x18` | `src+0x10` | |
| `+0x24` | `src+0x1c` (byte) | |
| `+0x28` | `src+0x20` | |

`ProresEnc_EncodeFrame(handle, frame_info, …)` is called at `0x230a2`, with the handle
loaded from `ctx+0x158`. Extra arguments are passed on the stack (`sp+0x14`, `sp+0x1c`).

So the HAL already plumbs profile, format and two dimensions from the device config —
the remaining question is which upstream field is width vs height, and whether the DUSS
stream config can supply 4:2:2.

| Phase | Work | Gate |
|---|---|---|
| 0 | Finish RE: config struct for `ProresEnc_Init` | struct documented; descriptor offsets confirmed |
| 1 | Device proof: run `test_hal_plenc` with a 2720×1530 422SP8 YUV file | valid `.prores` file, plays back |
| 2 | Determine 422 availability at video rate (`.sp` + `farmus`) | either option A or B chosen |
| 3 | Pipeline node: video frames → plenc, MOV mux | 10 s clip records and plays |
| 4 | UI exposure (patch or hook) | mode selectable from camera menu |
| 5 | Hardening: thermal, storage speed, battery, restore path | sustained 5 min record, reversible install |

Phase 1 is the decisive one: it proves the encoder works on *this* silicon with *our*
buffers, independent of the camera pipeline.

---

---

## 7. Phased plan

| Phase | Work | Gate |
|---|---|---|
| 0 | Finish RE: config struct for `ProresEnc_Init` | **done — §6.1** |
| 1 | Device proof: run `test_hal_plenc` with a 2720×1530 422SP8 YUV file | valid `.prores` file, plays back |
| 2 | Determine 422 availability at video rate (`.sp` + `farmus`) | either option A or B chosen |
| 3 | Pipeline node: video frames → plenc, MOV mux | 10 s clip records and plays |
| 4 | UI exposure (patch or hook) | mode selectable from camera menu |
| 5 | Hardening: thermal, storage speed, battery, restore path | sustained 5 min record, reversible install |

Phase 1 is the decisive one: it proves the encoder works on *this* silicon with *our*
buffers, independent of the camera pipeline.

---

## 8. Risks

* **Brick risk** — every phase beyond 0 needs write access to `/system` or a shell.
  1.5.2 is the final firmware; there is no vendor recovery path for a broken `/system`
  beyond the A/B mirror and `recovery.img`.
* **Thermal** — ProRes at 4:2:2 raises encoder load; sustained recording untested.
* **Storage** — 55 MB/s sustained requires UHS-II; card-dependent.
* **Warranty / terms** — modification is at the owner's risk; reverse engineering for
  interoperability is the stated purpose here.
* **Dead ends** — if the ISP cannot emit 422 at video rate, option B adds a copy per
  frame and may not sustain 29.97 within the power budget.

---

## 9. Appendix — addresses and files

| Item | Location |
|---|---|
| `duss_hal_plenc_encfrm` | `libduml_hal.so` `0xbd4c`, size 72 |
| `duss_hal_attach_dji_plenc` | `libduml_hal.so` `0x231b4`, size 204 |
| encoder implementation | `libduml_hal.so` `0x22f1c` |
| codec name table | `libduml_hal.so` rodata `0x374b4`: `prores`, `prores_splice8k`, `prores_8k` |
| source path string | `hardware/dji/duml/hal/codec/plenc/dji_plenc.c` |
| device registration | `/system/etc/dji.json` → `/dev/proresenc` |
| factory test | `/system/bin/test_hal_plenc` |
| CLI | `/system/bin/proresenc` |
| driver | `/system/lib/modules/proresenc_mod.ko` |

Related notes: `analysis/FINDINGS.md` (container decryption, firmware structure, GUI
architecture), `tools/cim_unpack.py` (offline CIM decryptor).

---

## 10. 1.5.2 audit (post-extraction)

**Encoder contract is unchanged in the shipping firmware.** `libduml_hal.so` 1.5.2 has
the same `duss_hal_plenc_encfrm` (72 B, `0xbd4c`) and `duss_hal_attach_dji_plenc`
(204 B) as 1.0.0 — only the attach address moved (`0x231b4` → `0x23214`). Everything in
§4 applies to 1.5.2 as written.

**Video UI in 1.5.2.** The GUI exposes exactly two video resolutions:

```
"2.7K, 2720x1530, 29.97 fps"
"Full HD, 1920x1080, 29.97 fps"
```

with enum names `E_Resolutions_PreviewVid1080` / `E_Resolutions_PreviewVid720`.

The two extra stream groups 1.5.2 ships (`video_1240P@2997` 2756×1240 and
`video_2064P@2997` 2756×2064) have **no UI labels at all** — they are pipeline support
without a user-facing option. That is a useful precedent: the firmware already contains
video modes the menu never offers.

**Not config-driven.** The GUI references no `/system` config for video settings; the
options are compiled (Qt Quick Compiler). There is no `PopoverVideo` — video settings
live in a settings page driven by `VideoProxyUI`.

**Implication for ProRes.** Adding a codec choice means either a binary patch to the
settings-page model, or a runtime hook — the same conclusion as §5/§6, now confirmed
against the final firmware. The encoder side needs no new work.
