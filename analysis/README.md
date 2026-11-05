# Hasselblad X1D II — tethering toolkit and reverse-engineering notes

Everything here talks to the camera directly over USB. No Phocus, no SDK, no vendor
tooling. The protocol was recovered by capturing Phocus's own traffic, then re-implemented
and verified against the hardware (X1D II 50C, firmware 1.5.2).

Nothing in this repository flashes, patches or writes persistent state to the camera. Every
command is one the camera's own software issues.

---

## What works

| capability | tool | notes |
|---|---|---|
| Live view | `viewer.py`, `liveapp.py` | 2756×2064 H.264, ~11 fps; video in its own ffplay window |
| Capture | `viewer.py`, `hbctl.py capture` | trigger + token handshake; file lands on the card |
| Exposure control | `viewer.py` | aperture / shutter / ISO, read back to confirm |
| All parameters | `hbctl.py` | read, write, describe; 87 advertised |
| Card listing | `hbctl.py storage` | both slots, no mass-storage volume needed |
| Reconnection | `viewer.py` | survives unplug/replug and camera reboots |
| Raw download | `hbctl.py download --session` | full 50 MP `.3FR` over USB, no card reader |

## Layout

```
lib/        shared library code - the camera driver and live-view helpers
  hbctl.py          protocol, Camera class, readable/writable parameters
  liveview.py       frame parsing, connect sequence, stream helpers
  parameter_table.txt   the 168 code -> name mappings
tools/      command-line tools, each runnable on its own
  hbctl.py          parameter read/write/dump, storage, capture, download
  liveview_replay.py, liveview_min.py   live-view experiments
  cim_unpack.py, parse_trace.py, watch_events.py, usbshim.c
capture/    the interactive apps
  viewer.py         tethering: controls + ffplay preview
  liveapp.py        preview only, no controls
analysis/   documentation and the raw traces it refers to
```

Run the app with `./run.sh`, or any tool as `python3 tools/<name>.py`.

## Quick start

```bash
# needs pyusb; falls back to /tmp/pylibs
python3 capture/viewer.py                  # controls window + ffplay preview
python3 tools/hbctl.py dump              # every parameter, value + descriptor
python3 tools/hbctl.py write Iso 400     # writes, then verifies by read-back
python3 tools/hbctl.py capture 10 5      # intervalometer: 10 shots, 5 s apart
python3 tools/hbctl.py storage           # list the card
```

The camera must not be held by Phocus — it claims the same USB interface.

Run it from your own terminal, not from inside another application's sandbox: it
writes captures to the folder you choose, and a sandboxed process can be denied
that write (which looks like the camera failing to save).

## Documentation

| file | what it covers |
|---|---|
| [`PROTOCOL.md`](PROTOCOL.md) | the USB protocol: framing, opcodes, value encoding, capture sequence |
| [`LIVEVIEW.md`](LIVEVIEW.md) | live view: frame format, start/stop, the viewer |
| [`HBCTL.md`](HBCTL.md) | using the command-line driver |
| [`STATUS.md`](STATUS.md) | research status, what was achieved, what is open |
| [`FINDINGS.md`](FINDINGS.md) | chronological log, including corrections |
| [`PARAMETERS-LIVE.md`](PARAMETERS-LIVE.md) | every parameter with its live value and descriptor |
| [`PRORES-FEATURE-NOTE.md`](PRORES-FEATURE-NOTE.md) | ProRes design note (parked, see below) |

## Ruled out — and why it matters

Negative results cost the most to rediscover, so they are recorded in full:

* **Hidden video modes** (2756×1240 / 2756×2064, present in the firmware pipeline) are not
  reachable over USB. Video resolution appears in no parameter: the 87-parameter dump is
  byte-identical at 1080p and 2720×1530. Reaching them would need an on-camera change.
* **Preview quality is fixed.** ~80 KB per 2756×2064 frame ≈ 7 Mbit/s. `0x0047` looks like
  a bitrate and does nothing; `LiveVideoMode` writes are rejected. Blocky-then-mushy is the
  encoder, not the transport.
* **ProRes** needs code running on the body (`proresenc`, `/dev/proresenc` exist but are
  unwired). The firmware is signed and there is no root path, so this is a
  vulnerability-research project, not a configuration change.

## Caveats on download

Download needs a **tether-transfer session** (the 181-command connect sequence), the camera
screen **on**, and the body in **shooting mode** — it refuses transfers while asleep or in a
menu. `hbctl.py download --session` does the whole sequence. See `PROTOCOL.md` §9d.

## Safety

* All device interaction is read-only or uses frames the camera's own software sends.
* `hbctl write` refuses unless it can read the value back, so a rejected write is visible
  rather than silent.
* The one genuinely risky path — modifying the camera — has not been attempted and the
  notes above explain why it is a different risk class.

## Design note: why the preview is a separate window

Tk's photo-image path costs real compositing time per frame — measured at 4.7 ms of
Python, but the window redraws made the controls feel dead. ffplay is a native video
renderer and stays smooth. So `viewer.py` puts the picture in ffplay and keeps only the
controls in Tk; the two are joined by a pipe carrying the same H.264 the camera sends.

`liveapp.py` is the same pipeline with no controls at all, useful as a sanity check.
