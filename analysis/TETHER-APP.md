# The tether app

`capture/viewer.py` — controls on the Mac, camera over USB, no Phocus.

```bash
./run_capture.sh            # or: python3 capture/viewer.py
./build_app.sh --open       # macOS bundle, see analysis/README.md
```

The window is only the controls; the picture is an ffplay process in its own window. That
split is deliberate — Tk's photo path is slow enough to make the controls feel dead.

---

## Controls

| control | what it does |
|---|---|
| the orange release | one shot; runs the bracket instead when the focus stack is on |
| `Space` / `Return` | same as the release |
| Aperture / Shutter / ISO / EV | greyed out according to the camera's mode |
| Lens | read only: name, focal length (zoom range and current), focus near to ∞, live |
| Focus stack | its menu: on/off, then frames (2–1000), step (Extra small…Extra large) and direction (infinity / near / symmetric); the tile shows e.g. `3 × XS → ∞` |
| `☐ Live view` | switch the preview on or off — see below |
| `☑ Save to` + folder | also pull the full-res `.3FR` to the chosen folder |
| footer | status · Captures (reopens the gallery) · exposure mode · battery |

The window is kept to one row of tiles and a thin footer, so the live view and the
captures below it lose as little of the screen as possible.

Exposure controls follow the dial: in **S** the aperture is greyed, in **A** the shutter is,
in **P** and full-auto both are, and M / Mq / video allow both. `Mq` has no live view by
design and the app says so rather than claiming a fault.

---

## Live view vs the cable release

**The camera will not fire from its own shutter button or cable release while the live view
stream is running.** Not "the event is missed" — no photograph is taken and nothing is
written to the card. This is a camera behaviour, not a bug in the app: Phocus is the same.

So the app has two modes, and `Live view` picks between them:

| Live view | shutter button / cable | preview | what happens on a press |
|---|---|---|---|
| on | dead | streaming | nothing at all |
| off | **works** | last captured still | the shot transfers and is shown |

The status line and the small hint under the checkbox always say which mode is armed:
`cable release armed - press the body shutter` or `body shutter blocked`.

Live view is **off by default**, because the cable release is the main workflow; the choice
is remembered in `~/.x1d_viewer.json`. Turning it on starts the stream immediately, and
turning it back off stops the stream, which restores the shutter within a fraction of a
second.

A body-triggered capture arrives as two async notifications:

* `0x45` — the body wrote the file to the card (sent even with no session)
* `0x40` — the body is offering that image to the host (needs the tether session)

The app reacts to `0x40`: it pulls the file with `op 7 / 0x0004`, extracts the embedded
JPEG, and shows it in a window of its own, so the still appears where the preview was. If
`Save a copy` is ticked the `.3FR` also lands in the chosen folder; otherwise the transfer
goes to `~/Library/Caches/X1D Tether/` and the 100 MB raw file is **deleted** as soon as
the still has been extracted from it, because the card already holds the original. The card
always keeps the original.

### Where the app writes

Nothing goes to `/tmp`, and nothing goes inside the app bundle:

| file | location | size |
|---|---|---|
| event log | `~/Library/Logs/X1D Tether/x1d-tether.log` | small |
| launcher stderr | `~/Library/Logs/X1D Tether/x1d-tether.err` | small |
| still shown in the window | `~/Library/Caches/X1D Tether/still.png` | ~4 MB, overwritten |
| unsaved `.3FR` in transit | `~/Library/Caches/X1D Tether/` | ~100 MB, deleted after use |
| `Save a copy` `.3FR` | the folder you chose | ~100 MB, kept |

Running from a checkout puts the log in `captures/` instead, so it sits next to the files it
describes — but only from a checkout; the packaged app never writes into `Contents/Resources`.

There is no session without live view in Phocus, which is why a working Phocus
cable-release trace contains no `op 1 / 0x0007` at all — the app gets the same effect by
replaying the session with that one command omitted (`replay_session(live=False)`).

---

## Lens and focus readout

The Lens tile's top line comes from three codes:

* `0x0078 LensProductName`. HC lenses on the XH converter send none, although the
  `.3FR` still gets `LensModel: HC 100`, so the tile then says "LENS".
* `0x002e FocalLength`, pushed as a zoom turns, in steps of about 5 mm. Through the
  converter it is already the effective length (the HC 100mm reads 80).
* The range in `0x002e`'s descriptor, e.g. `MLi40,85,1` for the HC 50–110 on the
  converter. Its maximum is low: that zoom reads up to 90, so the tile widens the range to
  include whatever it has seen.

**The converter is not visible over USB.** A full dump with the HC 50–110 on the XH
Converter 0,8, diffed against the XCD 45P dump in
[`PARAMETERS-LIVE.md`](PARAMETERS-LIVE.md), lists the same 87 codes. With or without a
session open, the only lens-related differences are:

| code | XCD 45P, direct | HC 50–110 + XH |
|---|---|---|
| `0x002e` FocalLength | 45, `MLi45,45,1` | 70, `MLi40,85,1`, effective (×0.8) |
| `0x0078` LensProductName | `'XCD 45P'` | empty |
| `0x0070` LensSerialNumber | `'16777215'` (0xFFFFFF, none) | `'1887'` |
| `0x0008` Aperture descriptor | 11 stops | 13 stops, f/3.5 … f/25 |

The rest are settings and counters. `0x0047`, range 2000–10000, looks like a white-balance
Kelvin value. `0x004b LensProductID` is readable (1600414 for the HC 50–110) but not
advertised.

So the converter can only be inferred: an HC lens on an X body is necessarily on an XH
adapter. Live, even that is unreliable. HC lenses send no name, and the serial number does
not tell them apart: the lens that reported 65 mm (probably an HC 80, ×0.8) sends
0xFFFFFF like the XCD. The `.3FR` names the lens (`LensModel: HC 100`) and records the
effective focal length (80 mm), but no readable tag names the converter. Maker note
`0x0018` differs between the two in byte 1 (`0x43` XCD, `0x03` HC) and byte 10 (`0x84`,
`0x80`); with one lens of each kind, it cannot be told whether either marks the converter.

The bottom line shows `0x0059 FocusPositionPercent`. Despite the name it is a fraction of
the lens's travel: 0.0 at closest focus, 1.0 at infinity, in steps of about 0.001. It is a
position, not a distance; the tile shows it as a percentage on a near-to-∞ track.

* **The body pushes it only inside a tether session.** While the ring turns, `0x42` events
  for `0x59` arrive several times a second. They come with live view on or off, but only
  once the session has been replayed. Outside one, the value reads `-1.0` and nothing is
  pushed, so the app now opens the session on connect in both modes (it used to wait for
  the first shot with Save a copy when live view was on). Phocus reads it once at startup
  and never polls; nor does the app.
* **A lens change disarms it.** Detaching the lens pushes `0x59 = -1.0`, `0x3d = 0` and
  focal length `0x2e = 0`. Reattaching pushes `0x3d = 1`, `0x2e`, `0x5b`, `0x71`, and the
  lens name in `0x78`, but focus then stays at `-1.0` with no events. Waiting, re-sending
  the focus describes (`0x54`, `0x59`, `0x5a`) and restarting live view do not bring it
  back; replaying the session does. The app replays it when `0x3d = 1` arrives (about
  5 s). Not on `0x78`: the HC 100mm on the XH Converter 0,8 attaches without a name.
  Which of the session's commands does the re-arming has not been narrowed down.
* **Only lenses that autofocus on the X bodies report a position.** Through the XH
  Converter 0,8 the HC 50–110 sends focus like an XCD lens; the HC 100mm and another
  lens reporting 65 mm send nothing. For those, `0x59` stays at `-1.0` even in a fresh
  session, no events come as the ring turns, and no other parameter moves. The tile
  then says "lens reports none" rather than a bare "--". What each attach sent:

  | lens | `0x2e` focal length | `0x4a` firmware | `0x5b` | focus |
  |---|---|---|---|---|
  | XCD 45P | 45 | – | 35 | yes |
  | HC 50–110 + XH | 40 → 60 → 65, follows the zoom | – | 70 | yes |
  | HC 100mm + XH | 80 (100 × 0.8) | v13.0.0 | not sent | none |
  | unknown, + XH | 65 | v10.0.0 | not sent | none |

  `0x5b` came only with the lenses that report focus, and goes to 32767 on detach, so it
  may mark AF capability; four lenses are too few to rely on it.
* **`0x005a FocusDistanceScale`** reads six zeros with the X Extension 9mm between body
  and lens, and `[40, 20, 60, 50, 110, 75]` with the XCD 45P mounted
  directly. It arrives in pieces after an attach (`[40, 20, 0, 0, 0, 0]`, then the full
  six), so it looks like three pairs read from the lens. It may be the lens's distance
  scale, which could turn the position into metres; not decoded yet.
* **The `.3FR` carries no focus distance either.** Four shots of the XCD 45P at positions
  1.0, 0.78, 0.50 and 0.0, compared tag by tag:
  * There is no `SubjectDistance`, and the scale's six numbers appear nowhere in the
    maker notes or the file header.
  * Maker note `0x0018`, byte 6, is a coarse focus step: 0, 3, 7, 14, which is
    `round(14 × (1 − position))`.
  * Maker note `0x0017`, bytes 14–15, a signed 16-bit value, also falls with closer
    focus: 3, −1, −4, −10. It may be a close-focus exposure correction; unconfirmed.
  * `Unknown_0x000f[0]`, which looked like focus at first, is the actual exposure time
    in milliseconds: 1.6 s → 1682, 3.2 s → 3564. It only tracked focus because the
    camera, in A mode, metered longer exposures closer in.

  Decoding the scale needs ground truth: the position (`0x59`) read with the lens
  focused on targets at measured distances.

---

## Focus stacking

The camera runs the bracket itself; the app sets the parameters and triggers it. Everything
below was confirmed by changing the camera's own menu and watching which parameter moved —
worth knowing, because two of them are easy to confuse:

| parameter | code | values |
|---|---|---|
| frames | `0x006b` | 2…1000 |
| step size | `0x006a` | 0–4 |
| direction | `0x006e` | 0 infinity · 1 near · 2 symmetric |
| initial delay | `0x006c` | 0, 2, 5, 10, 30, 60 s |
| exposure delay | `0x006d` | 0–16000 ms |
| drive mode | `0x0041` | **5 = focus bracketing** |

`0x006d` looks exactly like a step size in microns and is not — it is the exposure delay.
Setting it to 2000 sets a two-second delay.

**Live view must be off.** With the preview streaming the camera accepts the trigger but
ignores the drive mode, so a bracket silently becomes a single frame. The app therefore
stops the preview and *verifies the frame stream has stopped* before arming, retrying the
stop up to four times. This is the single most common cause of "it only took one picture".

---

## Error handling

Every failure path terminates quickly and says what happened. Nothing waits indefinitely.

| what you see | meaning | what to do |
|---|---|---|
| `stack: no frames - camera busy, try again` | trigger produced nothing at all | wait for the camera to finish writing, retry |
| `stack: 6/10 - lens limit reached?` | the camera stopped the sequence early | start from mid-focus, fewer frames, smaller step, or symmetric |
| `stack timed out - camera stopped responding` | the USB link wedged mid-bracket | power cycle the camera |
| `refused live view - wake the screen / exit menus` | the body refused preview | wake it, leave menus, wait if hot |
| `Mq - no live view by design` | the mode has no preview | none, this is correct |
| `turn off Save a copy and reconnect - tethered stacks go to the host` | a session owns the images | see below |

Timings: no first frame within 8 s → give up; 4 s of silence after a frame → stop early;
60 s absolute cap; the window watchdog frees the UI at `frames × 10 + 45` s.

---

## Known camera limitations

None of these are fixable from the host — they are the body's own behaviour, and they have
all been observed:

* **Sensor overheating is the root cause of most unexplained failures.** When hot, the
  camera refuses live view (`ack 0x09`), then starts dropping the USB link, ignoring
  shutter presses and wedging mid-operation. There is no thermal value in the protocol -
  `0x000b`, `0x000c`, `Device0Status` and `Device1Status` are all unimplemented or stay 0 -
  so the host cannot report temperature, only infer it from refusals. Let it cool; many
  "app bugs" this project chased turned out to be heat.

* **The lens can jam** mid-bracket and stop moving until the camera is rebooted.
* **The USB link can wedge** after a failure; reads then block and only a power cycle
  clears it. The recurring symptom is `Access denied` when another process — including
  macOS's own driver, after the camera presents a video-style interface — holds the
  interface.
* **Killing the client mid-session wedges the camera.** A `kill -9` skips the session
  teardown (`op 3 / 0x007a "I2,0,1"` then `op 1 / 0x0004 / 00 00 01`, which is what Phocus
  sends on disconnect) and the next connection fails with `Overflow` until the body is
  power cycled. Close the window, or send SIGTERM, so the teardown runs.
* **A bracket started too soon after a previous operation is ignored.** Give the camera a
  moment between captures.
* **A tether session changes where images go.** With `Save a copy` on, the camera sends
  frames to the host instead of the card, so bracketing produces nothing and the body
  reports a failed transfer. Stacks are refused while a session is active.

---

## UI notes worth not re-learning

* **The control window is forced topmost.** ffplay is a separate process and can otherwise
  sit over the controls and swallow every click.
* **A window can look active without being key.** macOS then delivers no clicks at all;
  clicking the title bar fixes it, and `_raise_controls()` does the same automatically.
* **If buttons seem dead, check the app log for `click …` lines first.** No line means
  the click never reached the process, and no widget change will help.
* **Nothing resizes the window.** The capture button keeps one label and the stack controls
  stay laid out; resizing on toggle is what made them feel unreliable.
* Native `tk.Button`/`Checkbutton` on aqua ignore `bg`, which is why the dark buttons are
  styled `tk.Label`s.
* **The release button is a Canvas, not a Button.** aqua draws even a `tk.Button` as its own
  rounded rectangle and ignores most styling, so a circle is not possible. The face is a
  stack of ~22 concentric ovals in `#F57513` — Canvas fills have no alpha, so every shade is
  pre-blended against the background by `_mix()`. That is what makes it read as a domed
  button lit from the upper left rather than a flat disc.
* **Hover and busy states redraw the same geometry** with a lighter or grey base, so the
  widget never changes size — the old button's text changed and the window moved.
