# Live view — protocol and viewer

The X1D II streams a live preview over USB that any host can consume. This is how it
works and how to drive it, recovered by tracing Phocus and then reproduced without it.

Applications: [`viewer.py`](viewer.py) is the full tethering window (preview, capture,
exposure controls); [`liveapp.py`](liveapp.py) is the minimal preview-only version.

---

## 1. Summary

| | |
|---|---|
| Endpoint | `0x84` bulk IN |
| Container | `LIVE` header + H.264 Annex-B payload |
| Codec | H.264 High profile, 2756×2064 |
| Rate | ~11–14 fps |
| Start | one command — `op 1` / `0x0007` / first byte `0x0c` |
| Stop | same command with all zero bytes |
| Decode | `ffmpeg` / `ffplay` handle it directly |

---

## 2. Frame format

```
off  size  field
 0    4    "LIVE"
 4    4    0x00000004          version / flags
 8    4    0x00000400          1024
12    4    payload length       ← bytes following the 24-byte header
16    4    width  = 2756
20    4    height = 2064
24    n    payload
```

The payload opens with a small sub-header containing the ASCII string `H264` at offset
`0x20`, then the elementary stream begins with a normal Annex-B access unit:

```
67 64 00 33 ac 4d …   SPS (High profile)
68 ea 8f 2c …         PPS
65 b8 21 df …         IDR slice
```

The sub-header is harmless — ffmpeg skips it and decodes cleanly. 137/137 frames decoded
in testing with no errors.

**The channel also carries control packets.** A 1 KB packet with the same `LIVE` magic but
`width = height = 0` is interleaved with the video frames:

```
total= 65320  payload=65296  w=2756 h=2064   <- video frame
total=  1048  payload= 1024  w=0    h=0      <- control packet
```

Feeding these to a decoder corrupts it (`pps_id out of range`) and they contain no SPS, so
a client that waits for SPS silently starves. **Skip any frame with `width == 0` or
`height == 0`.** This one filter was the difference between a stable preview and one that
stalled or glitched every few seconds.

Frame sizes vary with scene complexity: ~20 KB for a flat/black frame, ~80 KB typical,
~500 KB for a detailed one.

---

## 3. Starting and stopping

Live view is **a single command**, not the long handshake it first appeared to be:

```
start   op 1 / code 0x0007 / value 0c 00 00 00 00 00 00 00 00 00   -> ack 0
stop    op 1 / code 0x0007 / value 00 00 00 00 00 00 00 00 00 00   -> ack 0
```

That is the whole thing. No `TetheredMode`, no descriptor sweep, no `op 4` polling, no
prior state. The first byte acts as the switch: `0x0c` starts the stream, zero stops it.

Verified from a stopped camera: one command took the frame rate from `0.0 fps` to
`14.0 fps`, and an all-zero command put it back to `0.0`.

### How this was found, and what it corrects

Phocus sends 181 commands before the stream appears, which suggests a long negotiation.
It isn't one. Replaying prefixes showed nothing streams up to 167 commands and everything
streams at 168 — the extra command being `op 1`/`0x0007` above. **The other 180 are
irrelevant to live view.**

Two earlier conclusions in this file were wrong and are retracted:

* *"The stream needs a continuous `op 4` poll to sustain"* — false. It runs at ~11–14 fps
  unattended. The original observation came from a parsing bug that swallowed frames.
* *"Connecting takes ~30 s"* — false. That was the cost of replaying Phocus's handshake,
  which is unnecessary. Startup is now effectively instant.

---

## 4. Reading frames

No polling loop is needed. Just read:

```
while streaming:
    read 0x84, timeout ~80 ms      (accumulate into a buffer)
    parse complete LIVE frames out of the buffer
```

One caveat when joining a stream already in progress: the first frames may be P-frames
with no SPS/PPS, which the decoder rejects (`non-existing PPS 0 referenced`). Discard
frames until one contains an SPS (`00 00 01 67`) before feeding a decoder — `liveapp.py`
does this.

---

## 5. Viewing

`ffplay` does the whole job — decode, window, scaling:

```
ffplay -loglevel error -fflags nobuffer -flags low_delay -framedrop \
       -f h264 -window_title "X1D II live view" -i -
```

[`liveapp.py`](liveapp.py) connects, sustains the stream, and pipes each frame's payload
into that process:

```bash
python3 analysis/liveapp.py --seconds 60
python3 analysis/liveapp.py --record /tmp/preview.h264
```

Observed end to end: 223 frames in 20 s (11.1 fps) with a live window on screen.

Any other decoder works too — concatenate the payloads and ffmpeg produces a normal file:

```bash
ffmpeg -i /tmp/preview.h264 -vf "select=eq(n\,60)" -frames:v 1 frame.png
```

---

## 5b. Preview quality is fixed — it cannot be raised

The preview is visibly blocky when still and smears badly under motion. That is the
camera's encoder, not the transport: ~80 KB per 2756×2064 frame at ~11 fps is roughly
**7 Mbit/s**, which is low for 5.7 megapixels.

Two plausible levers were tried and neither moves it:

| attempt | result |
|---|---|
| `0x0047` (range 2000–10000, reads 5000 — looked like a bitrate) | write accepted, frame size unchanged: 82309 → 82386 bytes |
| `0x0051` LiveVideoMode, all 14 values | ack `0x0d`, resolution and frame size unchanged |

So the preview quality is a firmware constant. Nothing host-side can improve it, and
Phocus shows the same stream. In dim scenes the body also slows its preview exposure,
which adds real motion blur on top of the compression.

### Parsing, in one place

`liveview_stream.frames_from(buf)` is the canonical frame extractor: it resynchronises on
the `LIVE` magic, rejects implausible lengths, and **drops the control packets**. Both
`viewer.py` and `liveapp.py` use it, so there is no second copy to drift out of sync — an
earlier duplicate in `viewer.py` did not filter them and reproduced the stall.

## 6. Rough edges

* **~11 fps.** Phocus reaches ~27. Keeping more reads outstanding, or decoupling read from
  decode with threads, is the way to close the gap.
* **Read and decode share a thread**, so a slow decode back-pressures the stream.
