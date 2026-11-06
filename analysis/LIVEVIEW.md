# Live view — protocol and viewer

The X1D II streams a live preview over USB that any host can consume. This is how it
works and how to drive it, recovered by tracing Phocus and then reproduced without it.

Applications: [`viewer.py`](../capture/viewer.py) is the tethering app (controls in Tk, preview in an
a separate ffplay window); [`liveapp.py`](../capture/liveapp.py) is preview-only.

---

## 1. Summary

| | |
|---|---|
| Endpoint | `0x84` bulk IN |
| Container | `LIVE` header + H.264 Annex-B payload |
| Codec | H.264 High profile, 2756×2064 |
| Rate | ~22.5 fps (same as Phocus) |
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
12    4    packet length        ← the WHOLE packet, this 24-byte header included
16    4    width  = 2756
20    4    height = 2064
24    n    payload
```

**The length field covers the header too.** One bulk transfer on `0x84` is exactly one
packet, and its size equals this field — confirmed on all 1258 frames of Phocus's trace
and on the camera (`len=32611`, field `32611`). It was long read as the payload length;
that makes every packet look 24 bytes too long, so the parser swallowed the next packet's
header and skipped that frame. Result: half the frames (11 fps instead of 22.5), each with
24 junk bytes appended, and — because every P-frame is a reference frame — the decoder
predicting from frames it never got, which smeared anything that moved. See §5b.

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

*Caveat (2026-11-06):* these measurements were taken with the length-field bug above, so
the "total/payload" figures are off by 24 bytes. With the parser fixed, an 8-second
recording of 182 packets contained **no** zero-width packets. They may only appear in
other states (stream start/stop, a menu open); the filter stays because it is harmless.

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

[`liveapp.py`](../capture/liveapp.py) connects, sustains the stream, and pipes each frame's payload
into that process:

```bash
python3 capture/liveapp.py --seconds 60
python3 capture/liveapp.py --record /tmp/preview.h264
```

Observed end to end: 223 frames in 20 s (11.1 fps) with a live window on screen.

Any other decoder works too — concatenate the payloads and ffmpeg produces a normal file:

```bash
ffmpeg -i /tmp/preview.h264 -vf "select=eq(n\,60)" -frames:v 1 frame.png
```

---

## 5b. Preview quality — it was our parser, not the camera

**Retracted:** this section used to say the preview quality is a firmware constant and that
Phocus shows the same stream. Phocus gets the same bytes per frame but **twice the frames**:

| | fps | per frame (median) | bitrate |
|---|---|---|---|
| Phocus (`trace-liveview-2064.log`) | 22.6 | 65 KB | 11.9 Mbit/s |
| tether app, before the fix | 11.3 | ~72 KB | 6.7 Mbit/s |
| tether app, after the fix | 22.5 | ~65 KB | ~12 Mbit/s |

The camera was always sending 22.5 fps; the length-field bug (§2) threw every other frame
away. All P-frames carry `nal_ref_idc = 2`, so the skipped frames were references:
decoding the old parser's frames against the full stream gives 44–50 dB PSNR even on a
still scene (identical frames would be lossless), and the error grows with motion until
the next IDR (~30 frames). ffmpeg reports no error or warning for this - the gaps are
filled silently - which is why it went unnoticed.

The two levers tried earlier still do nothing: `0x0047` (2000–10000, reads 5000) leaves the
frame size unchanged, and every `0x0051` LiveVideoMode value is refused with ack `0x0d`.
`tools/liveview_rate.py` measures frame rate and bitrate per read strategy if this needs
re-checking; one 256 KB read at a time already reaches the full rate.

### Parsing, in one place

`liveview_stream.frames_from(buf)` is the canonical frame extractor: it resynchronises on
the `LIVE` magic, rejects implausible lengths, and **drops the control packets**. Both
`viewer.py` and `liveapp.py` use it, so there is no second copy to drift out of sync — an
earlier duplicate in `viewer.py` did not filter them and reproduced the stall.

## 5c. Thermal protection has no readable signal

When the sensor gets hot the body refuses live view (`ack 0x09` instead of `0x00`) and
displays "image sensor too hot". Several of the stalls chased during development were
this, not software.

There is no way to read it: `0x000b` (`SensorUnitTemperature`) is **not advertised** and
answers with an empty reply, as does `0x000c` (Accelerometer). `Device0Status` / `Device1Status` (range 0–5) were checked
*while the body was hot and refusing* and both still read `0`, so they do not carry thermal
state either. There is no thermal signal anywhere in the protocol. So the viewer infers it
from duration — a refusal that clears quickly is the screen being off; one that persists
past ~20 s is treated as thermal (`refused for Ns - likely too hot, let it cool`).

Confirmed preconditions the body enforces, all of which look identical from the host:
screen off, a settings menu open, or the sensor too hot. Only a power cycle reliably
clears the last one.

## 6. Rough edges

* ~~**~11 fps.**~~ Fixed: that was the length-field bug in §2, not read throughput.
* **Read and decode share a thread**, so a slow decode back-pressures the stream.
