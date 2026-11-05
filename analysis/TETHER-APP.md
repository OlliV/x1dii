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
| `●  CAPTURE` | one shot; runs the bracket instead when Focus stack is ticked |
| `Space` / `Return` | same as CAPTURE |
| Aperture / Shutter / ISO | greyed out according to the camera's mode |
| `☐ Focus stack` | enables the bracket controls |
| frames / step / direction | 2–1000 · Extra small…Extra large · infinity / near / symmetric |
| `☑ Save a copy` | also pull the full-res `.3FR` to the chosen folder |

Exposure controls follow the dial: in **S** the aperture is greyed, in **A** the shutter is,
in **P** and full-auto both are, and M / Mq / video allow both. `Mq` has no live view by
design and the app says so rather than claiming a fault.

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
* **If buttons seem dead, check `/tmp/viewer.log` for `click …` lines first.** No line means
  the click never reached the process, and no widget change will help.
* **Nothing resizes the window.** The capture button keeps one label and the stack controls
  stay laid out; resizing on toggle is what made them feel unreliable.
* Native `tk.Button`/`Checkbutton` on aqua ignore `bg`, which is why the dark buttons are
  styled `tk.Label`s.
