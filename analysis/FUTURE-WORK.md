# Future work

Tasks that are within reach of the existing toolkit. Everything here is host-side and
reversible — nothing requires modifying the camera. For the ideas that *do* require that,
see the closing section and [`UPGRADE-AND-SIGNING.md`](UPGRADE-AND-SIGNING.md).

Ordered by value versus effort.

---

## 1. Focus stacking — IMPLEMENTED, fallback still open

Done: `focus-stack` in the CLI and the Focus stack panel in the app drive the camera's own
bracketing, with frames, step size, direction and both delays. See
[`TETHER-APP.md`](TETHER-APP.md) for the controls, the confirmed parameter codes and the
error handling.

Still open: **`0x0059 FocusPositionPercent`** as a fallback for when the built-in sequence
runs out of lens travel. Stepping focus manually keeps the count and the starting point
under host control, which the camera's own bracket does not.

### Original notes

Autofocus is not worth pursuing: the body's AF is weak and the user does not use it. Focus
**stacking** is the interesting target, and the camera already implements focus bracketing
in its own menus — so this is not about building the feature, it is about **triggering it
from the host** so a stack can be run without touching the camera.

The parameters are all present in the advertised set:

| code | descriptor | meaning | units |
|---|---|---|---|
| 0x0041 `DriveModeMode` | `LI6,0..5` | drive mode; **5 = focus bracketing** | - |
| 0x006b `FocusBracketingFrames` | `LI57,2...1000` | number of exposures | count |
| **0x006a** | `MLi0,4,1` | **step size** | 0..4 = ExtraSmall...ExtraLarge, no physical unit |
| **0x006c** | `LI6,0,2,5,10,30,60` | **initial delay** | seconds |
| **0x006d** | `LI9,0,125...16000` | **exposure delay** | milliseconds (125 = 1/8 s) |
| 0x006e | `MLi0,3,1` | probably `FocusBracketingSequence` | unconfirmed |
| 0x006f | `MLi0,1,1` | probably `FocusBracketingWhenFinished` | unconfirmed |
| 0x0066 | `LI6,0,2,5,10,30,60` | unidentified, same scale as 0x006c | - |

All confirmed by changing the camera's own menu and watching which code moved.

The step size was initially assumed to be `0x006d`, because its values (125, 250, 500 …
16000) look like microns and it sits next to the focus parameters. That was wrong, and it
cost an hour: writing to it changed nothing on the camera, and it does not move when the
menu's step size changes.

`0x006a` is the real one. Confirmed by watching while changing the menu:

```
21:02:25  0x006a  0 -> 1     step size: Extra Small -> Small
21:03:39  0x006a  1 -> 2     step size: Small -> Medium
21:06:04  0x006c  0 -> 5     initial delay: 0 -> 5 s
21:06:53  0x006d  125 -> 2000   exposure delay: 1/8 s -> 2 s
```

Five values for five menu options. `0x006d` turned out to be the **exposure delay in
milliseconds**, not the step size — which is why its scale looked so plausible.

The lesson worth keeping: identify a parameter by changing the camera's own setting and
watching which code moves. Picking the code whose *numbers* look right got this wrong by
one parameter and cost an hour.

**There are no units.** The scale is five opaque names; the protocol exposes no distance
per step, and `FocusDistanceScale` reads six zeros. So "how much travel does N steps
consume" cannot be computed — which is why the safe approach is empirical: smallest step,
a few frames, start from mid-range, and accept that the camera may stop early with "Lens
limit reached".

### Drive modes — mapped on the camera

Confirmed by setting each value over USB and reading the camera's own display:

| value | mode | screen showed |
|---|---|---|
| 0 | Single | single shot |
| 1 | Continuous | continuous |
| 2 | Self Timer | 3 s timer |
| 3 | Self Timer (long) | 6 s timer |
| 4 | Exposure Bracketing | bracketing, 3 steps |
| 5 | **Focus Bracketing** | "focus bracketing, 10 steps" (10 = `FocusBracketingFrames`) |

**The host can select the drive mode** — all six writes were accepted and the screen
followed immediately. This is unlike `ExposureMode`, which is refused with `ack 0x0d`.

### Focus bracketing works — with two caveats, both camera-side

Tested on the body, watching the `0x45` new-file events:

* **Pressing the camera's shutter button runs the sequence.** A 2-frame bracket produced
  `B0002384` and `B0002385` before stopping with **"Lens limit reached"** — the bracket
  exhausted the lens's focus travel, which is a physical limit, not a protocol error.
  Fewer frames and the smallest step (125) still hit it from one starting position.
* **A host trigger *does* start the sequence — but only with live view OFF.** This was the
  misleading part. With the preview streaming, `op 1`/`0x0001` takes exactly one frame and
  the drive mode reverts to 0; with live view off, the same trigger runs the whole bracket.
  Nothing else changes, which is why it looked like a protocol limitation.
* **The mode self-reverts to 0** after the sequence, so it is re-armed for each run — which
  is convenient, not a problem.

So focus stacking **is scriptable**. The working recipe:

```bash
python3 tools/hbctl.py focus-stack 3 0        # frames, step  (0 = ExtraSmall)
python3 tools/hbctl.py focus-stack 4 2 0 125  # + initial delay 0 s, exposure delay 125 ms
```

which, in order: stops live view, sets `FocusBracketingFrames` (0x006b), sets
`FocusBracketingStepSize` (0x006d), sets `DriveModeMode = 5`, triggers once, and collects
the `0x45` new-file events until the expected count arrives.

`0x0059 FocusPositionPercent` remains the fallback for cases where the built-in sequence
runs out of lens travel — stepping focus manually keeps the count and starting point under
our control.

### Focus bracketing parameter names, from Phocus's HBUtil

`HBUtil.framework` carries these canonical names, which fill the five unlabelled codes
after `FocusBracketingFrames` in the live dump:

| code | live descriptor | current | probable name |
|---|---|---|---|
| 0x006b | `LI57,2…1000` | 2 | `FocusBracketingFrames` (confirmed) |
| 0x006c | `LI6,0,2,5,10,30,60` | 0 | `FocusBracketingInititalDelay` or `ExposureDelay` |
| 0x006d | `LI9,0,125,250…16000` | 125 | `FocusBracketingStepSize` — values look like microns |
| 0x006e | `MLi0,3,1` | 0 | `FocusBracketingSequence` — 4 values, 3 named strategies |
| 0x006f | `MLi0,1,1` | 0 | `FocusBracketingWhenFinished` |

The GUI also names the strategy and size enums, which is what those numeric ranges encode:

```
E_FocusBracketingStrategies_{ToNear, ToInf, ToNearToInf}
E_FocusBracketingStepSizes_{ExtraSmall, Small, Medium, Large, ExtraLarge}
E_ExposureStatus_FocusBracketingActive
```

`E_ExposureStatus_FocusBracketingActive` implies the camera reports bracket progress —
worth watching to confirm a run has started.

Next step: test `FocusPositionPercent` (`0x0059`), the only remaining route to a stack
under host control. Read it, write a small delta, and confirm the lens moves — then a
stack becomes: set position, fire, step, repeat.

Stacking itself happens afterwards in external software; the camera's job is only to vary
focus between exposures.

## 2. The camera's own automation is already there

We wrote an intervalometer in `hbctl` while the camera implements one:

| code | name |
|---|---|
| 0x0060 | `IntervalTime` (44 values, 2 s … 3600 s) |
| 0x0061 | `IntervalFrames` (58 values, 2 … 1000, plus `-1`) |
| 0x0062 | `IntervalInititalDelay` |
| 0x0063 | `IntervalWhenFinished` |
| 0x0064–0x0068 | exposure bracketing: amount, frames, sequence, step in M |
| 0x006b | `FocusBracketingFrames` |

Driving these from the app would give interval and bracketing sequences that the camera
executes internally — more accurate timing than our host loop, and bracketing that our
toolkit cannot currently do at all.

First step: `hbctl read` the group, then set a short interval and a small frame count and
watch the card. The open question is whether a separate command *starts* the sequence, or
whether setting the parameters and firing once is enough.

---

## 3. Downloading files already on the card — currently impossible

`op 7`/`0x0004` is scoped to a capture token from a `0x40` event, so the toolkit can only
fetch **the shot it just took**. Files already on the card need the card reader or the
camera's mass-storage mode.

Things worth trying before declaring it closed:

* the 14-byte value may take something other than a token — a path, an index into the
  directory listing, or an offset — and only the token case was explored;
* `0x0079 StorageExportedToPc` reads empty even for Phocus, so that route is dead;
* mass-storage mode is mutually exclusive with live view, which is the real reason this
  matters.

---

## 4. Video record start/stop — probably not exposed

Never tested. Video mode refuses the tether preview and `odindb` owns the camera services,
so recording is likely GUI-only. Cheap to check: enter video mode, then probe `op 4`
variants and the `0x0044 VideoType` / `0x0043 CameraMode` parameters while watching for a
state change. Low expected value, low cost.

---

## 5. Smaller items

* **Thumbnails for browsing.** The directory listing gives paths but no sizes or previews.
* **Multi-slot awareness.** Both slots list; copying a capture to both is `StorageMode`'s
  job, and we never confirmed its semantics beyond observing it as 1 (photo) / 0 (video).
* **Persistence of settings** across app restarts, beyond the save folder.
* **Notarisation for the app bundle** — currently ad-hoc signed, so it needs a right-click
  to open when copied to another Mac.

---

## 6. Comparing recent shots — tiled review

Asked for after the cable-release workflow settled. Right now a capture replaces the single
still window, so only the last frame is ever visible and comparing two takes means opening
files by hand.

**What it should do**

1. **Tile 2–4 consecutive shots side by side** — the last *n* captures laid out in a row, so
   the differences between takes can be judged at a glance.
2. **Click a tile to widen it.** Clicking one picture in the tiling blows that one up to fill
   the window; clicking it again restores the tiled view of the last *n*. So the grid is the
   resting state and a single picture is a temporary zoom, not a separate window.
3. **Arrow keys walk the history in single view.** Left/right (or up/down) swap to the
   previous/next capture while zoomed, without leaving single view.

**What it needs first**

* **A capture history.** The app currently keeps only `_seen_tokens` (a set, for dedupe) and
  a single overwritten `still.png`. It needs an ordered ring of the last *n* captures, each
  with its still path and token — a `deque(maxlen=n)` beside the existing bookkeeping.
* **Previews kept, not overwritten.** Stills should go to
  `~/Library/Caches/X1D Tether/stills/<token>.png` with the oldest pruned, instead of one
  fixed filename. Tiles want a smaller render (`sips -Z 900`) so four of them stay cheap;
  the single view can use the 1800 px version.
* **Tk image handling.** One `PhotoImage` per visible tile, and a reference kept or Tk drops
  the pixels — the existing `_still_window` already relies on this. Rebuilding the grid on
  every capture is fine at 4 tiles; a `Canvas` with image items is easier to re-lay-out than
  packed labels, and gives free hit-testing for the click-to-zoom.

**Constraints worth remembering**

* **The review only exists when live view is off.** Live view owns the preview area
  outright, so the tiling, the click-to-widen and the arrow-key history all belong to the
  cable-release mode. When the preview is streaming nothing is shown but the stream — the
  same split the app already has, where `Live view` off is the only state that displays a
  still at all. Concretely: the tiled view is the off-mode counterpart of the preview
  window, not something layered on top of it.
* The raw `.3FR` may already be deleted (that is deliberate — see
  [`TETHER-APP.md`](TETHER-APP.md)), so the gallery must be built from the extracted JPEG,
  not from re-reading the raw.
* Captures taken before this feature existed have no cached preview; backfill by extracting
  from the `.3FR` when `Save a copy` is on, and skip otherwise.
* Tokens are not guaranteed contiguous (a burst can skip numbers), so ordering must come
  from arrival order, not from sorting ids.

---

## Out of reach without a signing bypass

Listed so they are not re-litigated: ProRes (encoder present but unwired, *and* the
pipeline is 4:2:0 where ProRes needs 4:2:2), the unexposed 2756×1240 / 2756×2064 video
modes (not settable over USB, and the firmware cannot be replaced), and any GUI change.
See [`UPGRADE-AND-SIGNING.md`](UPGRADE-AND-SIGNING.md).
