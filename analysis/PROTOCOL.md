# X1D II host protocol — verified against the wire

Recovered by tracing Phocus 4.1.1's real USB traffic, then re-implemented and tested
against a live camera with Phocus closed. Everything in this file is **observed**, not
inferred, except where marked *(unverified)*.

> **Superseded.** The previous revision of this file described an "Fi envelope" and a
> "DUML heartbeat" path derived from static analysis of `CFiProtocol::Send`. That model
> was wrong: frames built from it were structurally valid but belonged to no protocol the
> camera speaks, which is why every one of them went unanswered. The disassembly work was
> sound — it just described a code path the camera never gets exercised on.

---

## 0. How it was captured

`HBTethered.framework` is the only image in Phocus.app that imports libusb, and it imports
exactly 23 symbols. A throwaway copy of the app had `LibUsb.framework`'s binary replaced by
a shim that defines all 23, logs every transfer, and forwards to the real dylib via
`dlopen`. Because the shim *is* the library HBTethered links against, two-level namespace
binding cannot bypass it — no interposing needed. Source: [`usbshim.c`](../tools/usbshim.c).

With that in place, connecting in Phocus and taking one photo produced
[`trace-phocus-capture.log`](trace-phocus-capture.log): 197 commands and 24 image
transfers. [`parse_trace.py`](../tools/parse_trace.py) decodes it to
[`trace-phocus-capture.txt`](trace-phocus-capture.txt).

The result was then re-implemented from scratch in [`hbctl.py`](../tools/hbctl.py) and tested with
Phocus quit. It reads firmware, serial and battery with no handshake at all.

---

## 1. Transport

| | |
|---|---|
| Device | `VID 0x2756` / `PID 0x0007`, config 1 |
| Interface | 3, alt 0 (vendor specific, `ff/00/00`) |
| `0x04` OUT | commands, one frame per transfer, sync write |
| `0x85` IN | command replies, 1024-byte reads, `timeout = 0` (blocking) |
| `0x84` IN | image data, up to `MaxBlockSize`, `timeout = 2000` |

Phocus keeps exactly one 0x85 read outstanding at all times and re-arms it from the
completion callback. Reads return the full 1024 bytes — the device pads past the frame.

Claiming interface 3 fails with `Errno 13` while Phocus is running.

---

## 2. Framing

```
host -> camera                       camera -> host
 04 00 08 05 <n> <payload n>          03 00 05 08 <n> <payload n>
```

| off | field |
|---|---|
| 0 | `0x04` request / `0x03` reply |
| 1 | `0x00` |
| 2,3 | `08 05` host→camera / `05 08` camera→host |
| 4 | payload length, one byte |
| 5.. | payload |

`08` and `05` read as source and destination identifiers. `n` is a single byte, so a
payload is at most 255 bytes — every command in the trace fits in 19.

### Payload

```
seq      u16 LE     one per request, incremented by the host
body     ...
```

**Request body:** `<op u8> <code u16 LE> [value]`

**Reply body:** `<op | 0x80 u8> <data>`

The reply's first byte echoes the request opcode with bit 7 set — this is what makes the
protocol self-describing. A reply whose opcode byte is `0x40` is an *asynchronous event*,
not an answer to anything.

---

## 3. Opcodes

| op | name | request body | reply data |
|---|---|---|---|
| `0x01` | write value | `code`, raw value | ack (`u32`, usually 0) |
| `0x02` | read value | `code` | typed ASCII (§5) |
| `0x03` | write string | `code`, ASCII | ack (`u32`) |
| `0x04` | indexed access | `code` [+ a `u32` index or `u8` selector] | `u32` |
| `0x05` | list parameters | *empty* | `u16[]` of codes |
| `0x06` | describe parameter | `code` | 2 flag bytes + descriptor (§6) |
| `0x07` | record write | `code` + 14-byte record | ack |

Notes on the less obvious ones:

* **`0x04`** was seen as `op 4 code 0x0001 value 07 00 00 00` (ExposureTime, returning 11)
  and as `op 4 code 0x0007 value 02` … `0d` (EvAdjust). Argument shape depends on the
  parameter. Effect *(unverified)*.
* **`0x07`** always carries exactly 14 value bytes. Phocus uses it for `MirroPosition`
  (0x0004), `FocusModeParam` (0x0002) and `Shutter` (0x0009) around a capture. The
  `2,770,004` byte count that later appears on the image channel is embedded in the
  `Shutter` record (`54 44 2a 00`), which suggests it announces an expected image size.
  Record layout *(unverified)*.

---

## 4. Reply semantics

A successful reply is `<0x80|op> <data>` with no explicit error flag: an unsupported
parameter replies with the right opcode and **empty data**. Example — reading the
unadvertised `FaceDetection` (0x0089):

```
-> 03 02 00 89 00        (op 2, code 0x0089)
<- … 82                  (op 2, no data)
```

So "unsupported" and "error" look alike. Enumerating with op 5 first is the reliable way
to know what exists.

Write acks are a `u32` that is not the written value: `MirroPosition` → 0, `Shutter` → 9,
`TetheredMode` → 13. Meaning *(unverified)*; treat as opaque.

---

## 5. Typed values (op 2)

Reads return ASCII with a one-letter type tag:

| tag | form | example |
|---|---|---|
| `i` | `i<decimal>` | `i150` |
| `f` | `f<16 hex digits>` — IEEE-754 double bit pattern | `f4059000000000000` = 100.0 |
| `S` | `S<len>,<text>` | `S17,/SD1/SD2:/SD1/SD2` |
| `r` | `r<n>` — a packed rational: `numerator << 16 | denominator` | `r1007d` = 1/125 |

The float tag is worth noting: the hex is the double's bit pattern, so `f4059…` decodes to
100.0 — ISO and EV values arrive as doubles.

The `r` tag is a rational, not a struct: `value = (numerator << 16) | denominator`. The
aperture table proves it — `40001` is 4/1, `30000a` is 48/10, `5f000a` is 95/10, `160001`
is 22/1, matching the names `4, 4.8, 9.5, 22`. Shutter speeds work the same way
(`1/125` = `r1007d`, `1.4` = `re000a`, `45` = `r2d0001`), which means a client can
construct valid values from the human-readable `ShutterName` table without needing the
camera to publish the parallel value list.

`0x7fffffff` is the "not applicable" sentinel — it is what `Shutter` reads while the body
is in aperture-priority, where the shutter is chosen by the camera.

---

## 6. Descriptors (op 6)

Two flag bytes, then `<L><kind><args>`:

```
01 01 4d 4c 69 30 2c 31 30 30 2c 31     ->  "MLi0,100,1"
```

| kind | meaning | example |
|---|---|---|
| `Mi` | min / max / step, integers | `Mi0,100,1` (Battery) |
| `Mf` | min / max / step, doubles | `Mf0,3ff0000000000000,…` |
| `I` | enumerated values | `I2,0,1` |
| `R` | range table, packed hex | `R11,40001,30000a,…` |
| `S` | string table, tab separated | `S232,1:08\t48:16\t…` |
| `F` | float value list | `F9,4059…,4069…` |

`ShutterName` is a 232-entry tab-separated table of every shutter speed the body
advertises — including `T` and `B`.

---

## 7. Connect sequence (what Phocus does)

1. claim interface 3, arm a 1024-byte read on 0x85
2. `op 3` write `TetheredMode` (0x007a) = `"I2,1,1"`
3. `op 1` write `MirroPosition` (0x0004) = `00 00 00`
4. `op 5` list parameters → 87 codes
5. `op 6` describe all 87, `op 2` read the ones it needs
6. arm reads on 0x84 — live view starts
7. on quit: `op 3` `TetheredMode` = `"I2,1,0"`, `op 1` `MirroPosition` = `01 00 00`

**Reads work without any of this.** `hbctl.py read` needs no handshake; the camera answers
op 2 / op 5 / op 6 on a freshly claimed interface. That is what makes read-only tooling
safe to run.

---

## 8. Image channel (0x84)

24 completed transfers in the captured session, 107 MiB total:

| size | count |
|---|---|
| 8,388,608 (= `MaxBlockSize`) | 12 |
| 2,770,004 | 2 |
| 6,312,448 | 1 |
| 255 / 4 / 0 | 9 |

The 8 MiB chunks begin with TIFF magic (`49 49 2a 00`, little-endian) — live view preview.
A capture is delivered on the same endpoint; the photo taken during the trace landed as a
67 MB `.fff` starting `4d 4d 00 2a` (big-endian TIFF).

---

## 9. Events

One example observed, immediately before the capture writes:

```
03 00 05 08 11 | 4d 00 | 40 | 04 42 1e 00 00 00 52 60 06 03 33 46 52 00
```

`seq = 0x004d`, opcode byte `0x40` (no high bit → not a reply), then a 14-byte record of
the same shape op 7 writes. Not decoded; a client must skip these rather than mistake them
for a late answer.

---

## 9b. Capture sequence (partly decoded)

```
1  op 1 / 0x0001  (empty)          host -> camera   shutter actuates, ack 0x81
2  event 0x40, data 04 <token u32>  camera -> host  token increments per shot (1e42, 1e43, 1e44 …)
3  op 7 / 0x0004  <token>          host -> camera   ack 0 — only accepted once a capture happened
4  op 1 / 0x0009  <token>          host -> camera
```

**All four steps verified end to end**: a capture performed this way produced
`/SD1/DCIM/102HASBL/B0002316.3FR` on the card. Steps 3–4 are compulsory — without them the
body fires the shutter and writes nothing.

Note the storage listing blob's first byte is the **entry count**, not a magic value: it was
`0x09` with 9 files and `0x0a` once the capture added a tenth.

## 9c. Storage transfer (`op 7`) — mapped, download not cracked

`op 7` is the bulk-data command. Sweeping the sub-command byte against a real file id:

| kind | result |
|---|---|
| 2 | ack `0`, returns a fixed 1024-byte info blob (identical regardless of id) |
| 9 | ack `0`, returns the directory listing (2,770,004 bytes on this card) |
| 4 | ack `8` — valid request, **refused** |
| 0,1,3,5–8 | ack `11` — invalid |

kind 4 is the file read. It is structurally accepted: putting a 32-bit id at value offset
2 yields `08` (refused) whereas a wrong offset yields `11` (invalid), so the layout is
right and a precondition is missing. Phocus sends it ~20 ms after a capture event with the
capture token as the argument.

**This firmware does not emit that event.** Capturing here produces `0x42` (parameter
change) and `0x45` (new file) events, but no `0x40` with a token:

```
op 0x45:  <01 00> <id u32> <path>
         01 00 06 5c 06 1f | /SD1/DCIM/102HASBL/B0002338.3FR
```

That is useful in its own right — a host can learn that a capture landed, its id, and its
path, without polling the directory. But without the token, kind 4 cannot be driven, so
## 9d. File download — solved

Download works, and the mechanism is the one described above:

1. `op 1` / `0x0001` (empty) fires the shutter.
2. In a **tether-transfer session** the camera answers with a `0x40` event carrying a token.
3. `op 7` / `0x0004` with that token (u16 at value offset 2) is accepted (ack `0`).
4. The raw streams on `0x84` in 8 MiB blocks until it goes quiet.

Verified: **106,722,304 bytes** (101.8 MiB) downloaded, starting `49 49 2a 00`; macOS
reports it as `format: 3fr`, `8272 × 6200` — the full 50 MP sensor. The file is the raw as
it sits on the card, not Phocus's `.fff` re-wrap.

**The session is the whole trick.** Outside it, the camera writes to the card and emits
`0x45` instead, and `kind4` answers `08`. The session is the 181-command connect sequence
Phocus performs; once established, `0x40` tokens keep coming for later captures in that
connection. `hbctl` does this with `session` / `download --session`.

Preconditions found the hard way — the body refuses transfers when:

* its screen is off (live view streaming keeps it awake), or
* it is sitting in a settings screen rather than shooting mode.

Until then, files come off via the card or the camera's mass-storage mode.

## 9e. Exposure modes (0x0003)

Read-only: a write is refused with ack `0x0d`. The body owns the dial; a host can only
mirror it. Values mapped by cycling the dial while logging:

| code | dial position | aperture settable | shutter settable |
|---|---|---|---|
| 9 | full auto (square) | no | no |
| 3 | P | no | no |
| 2 | S | no | yes |
| 1 | A | yes | no |
| 0 | M | yes | yes |
| 8 | Mq (M without live view) | yes | yes |
| 10 | video | yes | yes |

Two things that are easy to get wrong, both found the hard way:

* **Mq has no live view at all.** The camera refuses `0x0007` with ack `0x09`, exactly as
  it does when asleep or overheated. A client must not report that as a fault, and should
  not keep retrying — the preview will never appear in that mode.
* **The C1–C3 positions have no codes of their own.** They report whatever mode they are
  programmed to, so a C slot set to A reads `1` and follows the A rules. There is nothing
  to special-case.
* The sentinel `0x7fffffff` means "not applicable" and is what `Shutter` reads in A, but
  it is *not* sufficient on its own: in S the aperture still reports a real value, so the
  mode number has to drive which control is available.

## 9f. The transfer stream is not self-delimiting

Three things must be true of a client that writes the downloaded bytes to a file. Getting
any of them wrong produces a `.3FR` that looks plausible — it has a valid TIFF header and
may even preview in the Finder — but no raw converter will open it.

1. **Nothing else may read `0x84` while the transfer runs.** It is the same endpoint as the
   live view. A preview reader competing with the download steals bytes out of the middle:
   files arrive truncated, or beginning mid-frame (`not a TIFF`).
2. **The stream may start with a stray preview frame.** Resync on the TIFF header
   (`II*\0` / `MM\0*`) rather than assuming the first byte belongs to the file.
3. **Stop at the length the TIFF declares.** The camera keeps sending past the end of the
   file; a naive "read until quiet" loop appends tens of megabytes of junk. A 3FR declares
   its own size: IFD0's strips plus the strips of each SubIFD listed in tag 330, taking the
   largest `offset + bytecount`.

A healthy file has `file size == declared extent`, to the byte. `lib/hbctl.py` implements
this as `tiff_extent()` plus resync in `download()`; the viewer pauses its reader via
`self.downloading` for the duration.

Observed sizes on an X1D II 50C: raw SubIFD is 8384x6304 uncompressed 16-bit, the 3FR is
about 107 MB, and the embedded 4136x3100 JPEG preview sits just after the header.

## 9g. Camera-initiated capture (shutter button / cable release)

Captured from a Phocus trace: pressing the body's shutter while tethered makes the camera
announce the shot itself. This is how a tether app reacts to the physical button.

```
camera -> host   op 0x40 (async)   04 <id u32 LE> 00 <3 bytes> <S3 "3FR">
host -> camera   op 7   kind 0x0004   value: 00 00 <id lo> <id hi> + 10 zero bytes
host -> camera   op 1   code 0x0009   value: <id u32 LE>          (acknowledge / consume)
```

Observed, three shots taken with the shutter button and the cable release:

| event payload | id | fetched with |
|---|---|---|
| `04 43 1e 00 00 00 a8 5f 06 03 33 46 52 00` | `0x1e43` | `op 7 / 0x0004 / 0000 431e …` |
| `04 03 1f 00 00 00 6a b6 06 03 33 46 52 00` | `0x1f03` | `op 7 / 0x0004 / 0000 031f …` |
| `04 04 1f 00 00 00 50 b6 06 03 33 46 52 00` | `0x1f04` | `op 7 / 0x0004 / 0000 041f …` |

Notes:

* The id is the **same token** our own captures use, so `download(token, path)` works
  unchanged on a camera-initiated shot.
* The trailing `03 33 46 52 00` is a length-prefixed `"3FR"` — the file format, so other
  formats presumably appear here too.
* Phocus is streaming live view while this happens; the event arrives interleaved with
  video frames, so a reader must parse control packets out of the stream rather than
  assuming each USB read is a control packet.
* This requires the tether session, exactly like our own captures.

**Client bug that hid this for hours.** Replies and async events share endpoint `0x85`, and
a normal command wait reads from it looking for its own sequence number. The original
implementation discarded anything that was not its reply:

```python
if status == 0x40:
    continue        # async event, not an answer  <- silently lost the capture event
```

Since the app polls parameters continuously, there was almost always a call in flight, so
every event was thrown away and it looked as though the camera never sent one. A client must
dispatch `0x40` payloads (and `0x45` file events) rather than dropping them, and should not
let a reader thread and command replies steal each other's packets from the same endpoint.

**Async notifications exist beyond 0x40/0x45.** The camera also sends status `0x42` frames
whose payload is a typed value, e.g. `3f 53 34 2c 31 2f 31 33` = `"?S4,1/13"` - a shutter
speed. These arrive when the body changes a setting itself (metering while shooting). They
were being discarded by the same reply-matching code, so a client should treat the whole
`0x40..0x4f` range as notification traffic, not just `0x40`.

**Status 2026-11-05 (late): SOLVED - the body shutter is blocked by live view.**

The cause was not the session at all. The camera refuses to fire from its own shutter
button or cable release **while the live view stream is running**. Stop the stream and the
shutter comes straight back; start it and the shutter is dead again. Phocus behaves exactly
the same way, which is why a working Phocus cable-release session contains no
`op 1 / 0x0007` at all: compare `trace-phocus-cable.log` (cable release, no live view ever
started, `0x40` arrives) with `trace-liveview-2064.log` (live view session, no capture
events anywhere in it).

Confirmed sequence, captured with live view off (`analysis/trace-phocus-cable.log`):

```
0x45  01 00 <dirId u32> "/SD2/DCIM/102HASBL/B0002516.3FR"     file written to the card
0x45  01 00 <dirId u32> "/SD1/DCIM/102HASBL/B0002516.3FR"     second slot, same frame
0x40  04 <id u32> 00 6c b2 06 03 "3FR" 00                     image offered to the host
```

Then the host pulls it with `op 7 / 0x0004` (`00 00 <id16> 00*10`) exactly as for a
host-triggered capture, and acknowledges with `op 1 / 0x0009 <id>`. The 112 MB transfer
completes in about a second over USB 3.

Two practical consequences for a client:

* `0x45` (file written) arrives **without a session** - it is the body announcing its own
  card write. It is the reliable "did the shutter fire" witness, and it is what made the
  earlier experiments look negative: they were watching a storage listing that returns
  empty while a session is active.
* `0x40` (image offered) requires the tether session. No session, no transfer - the file
  stays on the card. So a cable-release client must replay the session *and* keep live
  view off.

They are not quite as exclusive as they look, but close: if live view has run in the
session and is then stopped, the shutter works again and `0x40` still arrives, so the host
side can switch. What must never happen is pressing the shutter *while frames are
streaming*.

**Historical notes (kept because they explain the dead ends).**

* Our client had never actually received a `0x40` because two separate bugs stacked: async
  events were dropped by reply matching (fixed), and `replay_session()` read its command
  list from `analysis/trace-liveview-2064.log`, which is **not in the app bundle** - so the
  session silently failed and nothing was ever offered. The sequence is now checked in as
  `lib/session_sequence.json`, and the failure surfaced because a worker thread was dying
  on a Tk call (`main thread is not in main loop`) before it could log anything.
* `_ensure_session()` now reports how many replay commands went unanswered instead of
  claiming success unconditionally. One miss (`describe 0x0009`, which answers in several
  frames) is normal and is tolerated up to `SESSION_TOLERANCE`.
* The earlier `TetheredMode` experiment was inconclusive, not negative: it watched a card
  listing that is empty during a session, so it could not have detected a capture either
  way. It is superseded by the live view finding above.


## 10. Tooling

```bash
python3 tools/hbctl.py list                 # 87 advertised parameters
python3 tools/hbctl.py read FirmwareVersion SerialString Battery
python3 tools/hbctl.py dump                 # every parameter: value + descriptor
python3 tools/hbctl.py describe ExposureTime
python3 tools/hbctl.py raw 2 0x0089         # arbitrary op/code
```

Writes are refused unless `--write` is passed. `pyusb` is needed; see `STATUS.md` for the
`/tmp/pylibs` note.

---

## 11. Open questions

1. Op 4 and op 7 semantics — both appear in the capture path and neither is decoded.
2. The write ack `u32` (0 / 9 / 13).
3. Whether the length byte allows >255 bytes, and if longer values chunk.
4. Event record layout.
5. `LiveVideoMode` (0x0051) advertises 14 modes; the firmware's video pipeline has only 4
   groups. Mapping unknown — and it is the most interesting lever for the hidden
   resolutions. See [`PRORES-FEATURE-NOTE.md`](PRORES-FEATURE-NOTE.md).

---

## 12. Recovering this again

If the trace ever needs re-taking: copy Phocus.app, back up
`Contents/Frameworks/LibUsb.framework/Versions/A/LibUsb` to `LibUsb.real` there, build
[`usbshim.c`](../tools/usbshim.c) with

```
clang -dynamiclib -arch arm64 -arch x86_64 -I/opt/homebrew/include/libusb-1.0 \
  -install_name @rpath/LibUsb.framework/Versions/A/LibUsb -o LibUsb usbshim.c
```

and drop it in place, ad-hoc signing both. Then `open` the copy and watch
`/tmp/libusb_trace.log`.

One trap worth recording: the shim must not clear the stored original callback when a
transfer completes. Phocus re-submits the same `libusb_transfer` from inside its callback,
so clearing it silently kills the 0x85 read loop after one packet — which is exactly what
happened on the first run and looked like a camera that stopped answering.
