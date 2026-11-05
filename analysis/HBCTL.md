# hbctl — host driver for the X1D II

Talks to the camera directly over USB. No Phocus, no SDK, no mass-storage volume.

```
python3 tools/hbctl.py <command> [args]
```

It needs `pyusb`; if that isn't importable it falls back to `/tmp/pylibs` (see
`STATUS.md`). Phocus must be quit — it holds interface 3, and `claim_interface` fails
with `Errno 13` while it runs.

---

## Commands

| command | what it does |
|---|---|
| `list` | enumerate the 87 advertised parameters |
| `read <name\|code>…` | read values (`FirmwareVersion`, `0x001b`, `Iso`) |
| `write <name> <value>` | write, then verify by read-back |
| `describe <name>…` | descriptor: range, enum, or table |
| `dump` | every parameter with value and descriptor |
| `watch [<name>…]` | poll and print changes with timestamps |
| `snapshot <file.json>` | full state as JSON |
| `focus-stack [frames] [step] [init_s] [exp_ms]` | focus bracketing; step 0=ExtraSmall … 4=ExtraLarge (live view off) |
| `storage` | list the card |
| `raw <op> <code> [hex]` | arbitrary frame; write ops need `--write` |

```
$ python3 tools/hbctl.py read FirmwareVersion Battery Iso
0x001b FirmwareVersion        '1.5.2'
0x002f Battery                95
0x000a Iso                    100.0

$ python3 tools/hbctl.py write Iso 400
Iso                    400 -> 400.0   (was 100.0, ack 00 00 00 00)
```

---

## Value encoding

Values cross the wire as typed ASCII in *both* directions, and writes reuse the read
format — that is the single most important thing to know:

| tag | read | write |
|---|---|---|
| `i` / `I` | `i150`, `I7,0,1,2,…` | `I1,150`, `I7,0,1,2,…` |
| `f` | `f4059000000000000` (double, big-endian bits) | `f` + the same hex |
| `S` | `S17,/SD1/SD2` | `S<len>,<text>` |
| `r` | `r40001` | `r<text>` |

`write` reads the parameter first, takes its tag, and encodes to match. It then reads
back and reports whether the value stuck, so a rejected write is never silent:

```
Iso                    400 -> 400.0   (was 100.0, ack 00 00 00 00)
SelfTimerTime          6 -> 6   (was 3, ack 00 00 00 00)
```

If the encoded form is ever wrong, the camera answers with a non-zero ack and the value
does not change — that is the failure mode to look for, not an exception.

---

## Opcodes, briefly

| op | role |
|---|---|
| 1 | command / trigger — `MirroPosition`, `Shutter`. These *do* things; the value isn't stored. |
| 2 | read value |
| 3 | **write value** (the one you want for settings) |
| 4 | indexed access — not decoded |
| 5 | list parameters |
| 6 | describe parameter |
| 7 | data block — storage listing |

Op 1 and op 3 are easy to confuse. `MirroPosition` accepts a write and reports ack 0, but
reading it back still gives the resting value because the command already happened. Only
op 3 changes stored settings.

---

## Verified on hardware

* `FirmwareVersion 1.5.2`, `SerialString VQ20200944`, `Battery 95`.
* Integer write with read-back (`SelfTimerTime` 3 → 6 → 3).
* Double write with read-back (`Iso` 100 → 400 → 100).
* Full 87-parameter dump, and a 86-key JSON snapshot.
* **Capture end to end** — `capture 1` fires the shutter, completes the token handshake,
  and the file lands on the card (`B0002316.3FR`).
* Storage listing (9 paths on this card) — **photo mode only**; in video mode the request
  is accepted but the blob is not a listing, because storage is locked for recording.

---

## Gotchas

* **Video mode changes everything.** `CameraMode` (0x0043) 0 → 1 rewrites the parameter
  set: ExposureMode → 10, Shutter 17 values, ShutterName 232 → 104 entries, ISO 10 → 6
  steps, e-shutter forced, EV adjust disabled. Take a `snapshot` before and after if you
  care.
* **Storage listing needs photo mode.**
* Reply byte `0x40` is an asynchronous event, not an answer; the reader skips those rather
  than mistaking them for a late reply.
* Reads work without any handshake. Writes also work without one — no `TetheredMode`
  negotiation is required, which is why the toolkit can run standalone.

---

## Not yet available

* **File download.** The read still refuses, so images can be listed but not pulled over
  USB — the mass-storage volume or a card reader is the way to get files off today.
