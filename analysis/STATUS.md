# X1D II research — status and handoff

---

## 0. Resume here

**The protocol is solved and there is a working toolkit.** `analysis/hbctl.py` talks to the
camera with Phocus closed — reads, writes (verified by read-back), descriptors, snapshots
and storage listing. Usage: [`HBCTL.md`](HBCTL.md).

```bash
python3 analysis/hbctl.py list          # 87 advertised parameters
python3 analysis/hbctl.py dump          # every parameter: live value + descriptor
python3 analysis/hbctl.py read FirmwareVersion SerialString Battery
python3 analysis/hbctl.py write Iso 400 # writes, then reads back to prove it stuck
```

Verified on hardware: `FirmwareVersion 1.5.2`, `SerialString VQ20200944`, `Battery 95`,
integer and double writes round-tripping, 87-parameter dump, card listing.

The single most useful thing learned late: **values are typed ASCII in both directions, and
writes reuse the read format** — a write is `op 3` carrying `"I1,400"`, exactly the shape
the camera reads back. Write acks are `0` when accepted and non-zero when not, and
`hbctl write` checks the read-back rather than trusting the ack.

The open problem is no longer "how do we talk to it" but "what do we make it do". Video
resolution turned out to be a dead end (§1.5) — it isn't on the wire at all. What is left
is capture triggering and file download (§1.7).

Environment notes:
* camera must not be held by Phocus (`claim_interface(3)` fails while it runs);
* `libusb-package` + `pyusb` are installed under `/tmp/pylibs` (may be gone after reboot);
* all firmware extraction artefacts are reproduced by `cim_unpack.py`;
* the old "Phocus x86_64 offsets" note is obsolete — the protocol was recovered by
  capturing real traffic, see `PROTOCOL.md` §0 and §12.

Last updated: 2026-11-05. Firmware under study: **X1D II 50C v1.5.2** (final release,
2022-08-26). No camera modification was performed at any point; all device interactions
were read-only or frames identical to ones Phocus itself sends.

---

## 1. What was achieved

### 1.1 Firmware is fully unpacked

`X1D_II_50C_v1_5_2.cim` (233,878,016 bytes) decrypts and extracts end to end.

* Container: `VHABCIM` public header + AES-128-CBC payload.
* **Key detail:** the payload is encrypted in **4096-byte chunks, each restarted from a
  fixed IV** — not one continuous CBC chain. Chunk grids are per item.
  * key `93f82ca244ab29e0366934d56713eea2`
  * iv  `bd972536e78e1c3ebf385e01ae4adb52`
* Verified against the device: the `hbmanual_pack.tar` PDF extracted from the CIM is
  **byte-identical** to the copy the camera exposes on its `HBMANUAL` USB volume.
* All 17 OTA entries extract with valid CRC-32; `system.new.dat` → `system.img`
  (256 MiB ext4) builds cleanly.

Tool: [`cim_unpack.py`](cim_unpack.py) (`info` / `decrypt` / `extract`).

### 1.2 Platform facts established

| Question | Answer |
|---|---|
| OS | Android 6.0, `userdebug`, test-keys, ARMv7, DJI `eagle_ec1704` |
| GUI | `victory-gui-static`, Qt 5.11.1 **static**, QML compiled to C++ (no QML on disk) |
| Video (1.5.2) | 1080p, 2720×1530, plus unexposed 2756×1240 and 2756×2064 groups — all 8-bit 4:2:0 |
| 10-bit | **absent** — no 10-bit pixel format anywhere in the 1.5.2 pipeline profile |
| ProRes | full encoder stack present (`proresenc`, `libproresenc.so`, `proresenc_mod.ko`, `/dev/proresenc` registered in `dji.json`) but **zero references** in camera/GUI → unwired |
| HEVC | silicon supports it; no `h265`/`hevc` name in the HAL or framework → not selectable |

### 1.3 Host protocol — solved and re-implemented

Captured by replacing `LibUsb.framework` inside a throwaway copy of Phocus with a logging
shim (§0). The camera speaks a plain request/reply protocol over interface 3:

```
host → camera   04 00 08 05 <n> │ <seq u16> <op> <code u16> [value]
camera → host   03 00 05 08 <n> │ <seq u16> <0x80|op> <data>
```

* `0x04` OUT = commands, `0x85` IN = replies, `0x84` IN = bulk data.
* Reply byte echoes the request opcode with bit 7 set; `0x40` there is an async event.
* Ops: `1` write · `2` read · `3` write string · `4` indexed · `5` list · `6` describe · `7` data block.
* Reads return typed ASCII — `i150`, `f<16 hex>` (double bits), `S<len>,<text>`, `r<a,b,c>`.
* Reads need **no handshake**; the camera answers on a freshly claimed interface.
* Driver: [`hbctl.py`](hbctl.py). Full detail and the trace: [`PROTOCOL.md`](PROTOCOL.md).

Live parameter map captured on hardware: [`PARAMETERS-LIVE.md`](PARAMETERS-LIVE.md) — 87
advertised parameters, each with current value and descriptor.

### 1.4 Storage access over USB

`op 7` with code `0x0009` fetches a storage listing: the camera returns a length-prefixed
path blob on `0x84` (2,770,004 bytes for this card). Parsed, it yields:

```
/SD1/DCIM/102HASBL
/SD1/DCIM/102HASBL/B0001003.3FR … B0002302.3FR
```

So the card can be browsed without Phocus and without the mass-storage volume. The ack for
`op 7` is a `u32`; `0` means accepted, `8` means refused. `op 7` code `0x0004` (a bulk read)
is refused with `8` unless some prior state exists that Phocus establishes, so **file
download is not yet working** — only listing.

### 1.5 Video mode — probed, and **not reachable over USB**

The camera's video menu offers 1920×1080 and 2720×1530 at 29.97 (plus the two unexposed
groups the firmware contains). Switching between them changes nothing the host can see:

| probe | result |
|---|---|
| full 87-parameter dump at 1080p vs 2720×1530 | **byte-identical** |
| `LiveVideoMode` (0x0051) read | returns `I14,0,1,…,13` — the allowed list, never the current value |
| 12 unadvertised codes (`ImageFormatOption` 0x009c, `ImageFormat` 0x0080, `CameraCapabilities` 0x001a, `Configuration` 0x002c, …) | all empty, i.e. nonexistent |
| `LiveVideoMode` writes (op 3 `"I1,n"`, op 1, op 4) | acks 13 / 9 / 11, none `0` |

Video mode itself *is* visible — `CameraMode` (0x0043) goes 0 → 1 — and the parameter set
reshapes around it (ExposureMode → 10, Shutter 17 values, ShutterName 232 → 104 entries,
ISO 10 → 6 steps, EShutter forced 1, EvAdjust/FlashEvAdjust disabled). But **no parameter
carries resolution or frame rate**, and Phocus cannot tether video at all (black frame,
controls greyed out).

Conclusion: the video resolution lives entirely inside the camera's own GUI and pipeline
selection. It is not settable, or even observable, through the host protocol. Reaching the
hidden 2756×1240 / 2756×2064 groups requires modifying the camera (GUI binary or pipeline
config), not the host.

### 1.6 Live view (photo mode) — captured, format known

With Phocus connected in photo mode the camera streams continuously on `0x84`, ~27 fps,
~66 KB per frame, 1261 frames captured. Header:

```
4c 49 56 45   "LIVE"
04 00 00 00   version/flags
00 04 00 00   1024
a0 60 01 00   payload length
c4 0a 00 00   width  = 2756
10 08 00 00   height = 2064
```

Every frame is 2756×2064 — full sensor width, exactly the `video_2064P@2997` group. That is
the *preview* size and does not follow the video setting (it stayed constant across a
resolution change), so it is not a usable readout for the recording resolution.

The stream only runs while Phocus drives it; re-arming `0x84` from `hbctl` alone gets
nothing, and the 181-command connect sequence that precedes it is in
[`trace-liveview-2064.log`](trace-liveview-2064.log) if it is ever worth replaying.

### 1.7 Where device work stands

Reads, storage listing, still-photo parameter writes and **capture** are all working
end to end. What is not:

* **Video resolution.** Settled negative, see §1.5 — it is not on the wire at all.
* **File download.** `op 7`/`0x0004` (bulk read) still refuses with ack `8`, so images can
  be listed but not pulled. Phocus establishes some state first that has not been found.

---

## 2. What is still open

1. **Video modes.** Closed as far as the host protocol goes (§1.5). Reaching the hidden
   2756×1240 and 2756×2064 groups would need an on-camera change — GUI binary or pipeline
   config — not a host-side one. Parked unless that becomes acceptable.
2. **ProRes integration.** Encoder contract known (`duss_hal_plenc_encfrm`,
   `ProresEnc_Init`); needs 4:2:2 frames and MOV muxing. Design note:
   [`PRORES-FEATURE-NOTE.md`](PRORES-FEATURE-NOTE.md).
3. **GUI exposure.** The UI is compiled; adding a menu entry means patching
   `victory-gui-static` or hooking it at runtime.

---

## 3. Reproduction

```sh
# unpack the firmware (needs pycryptodome)
python3 analysis/cim_unpack.py info    X1D_II_50C_v1_5_2.cim
python3 analysis/cim_unpack.py extract X1D_II_50C_v1_5_2.cim /tmp/fw152

# read the ext4 image (needs dissect.extfs)
python3 -c "from dissect.extfs import ExtFS; fs=ExtFS(open('/tmp/fw152/system.img','rb')); print(fs.get('/').listdir())"
```

Scene notes for the Phocus side are in `PROTOCOL.md` §7 (arch offsets, tooling).

---

## 4. Evidence boundaries

* All conclusions about the *camera* come from its firmware image and from read-only
  USB enumeration/mass-storage access.
* Live protocol claims come from static analysis of Phocus 4.1.1; only frame *writes*
  were attempted, never anything that alters persistent state.
* No signature bypass, no flashing, no bootloader interaction was attempted.

---

## 5. File map

| file | contents |
|---|---|
| `cim_unpack.py` | offline CIM decryptor / extractor |
| `FINDINGS.md` | chronological research log, including corrections |
| `PROTOCOL.md` | host protocol, verified against the wire |
| `HBCTL.md` | how to use the host driver, value encoding, gotchas |
| `hbctl.py` | the driver itself (read / write / describe / dump / watch / storage) |
| `PARAMETERS-LIVE.md` | every parameter with its live value and descriptor |
| `usbshim.c` | LibUsb replacement used to capture Phocus's traffic |
| `parse_trace.py`, `watch_events.py` | decode a trace; list async events |
| `trace-phocus-capture.log` | raw libusb trace of connect + one capture |
| `trace-liveview-2064.log` | raw trace of a live-view session (2756×2064) |
| `PRORES-FEATURE-NOTE.md` | ProRes design note: contract, gaps, phased plan |
| `parameter-table.txt` | 170 parameter codes |
| `extracted/hbl-*.sh` | upgrade scripts recovered from 1.5.2 |
