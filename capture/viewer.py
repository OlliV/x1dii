#!/usr/bin/env python3
"""Tethering control window for the Hasselblad X1D II.

Video goes to ffplay in its own window; this window is only the controls. That split is
deliberate: Tk's photo-image path costs real compositing time per frame and made the
buttons feel dead, while ffplay is a native video renderer and stays smooth.

Because ffplay is a separate process (and a separate macOS app), its window is opened just
below the controls so the two never overlap. The controls are deliberately NOT topmost:
`-topmost` puts a Tk window at the utility window level, which macOS will not move between
desktops, and it keeps the controls visible while another app is active - inviting clicks
that Tk then drops (below).

Tk 8.6.12, which the python.org 3.11 installer ships, discards any mouse press that arrives
while the app is not the active one; only a title-bar click or a key press activates it
cleanly. Tk 8.6.13 removed that. So _raise_controls() activates the app at startup and
again when ffplay's window first appears and takes focus. If buttons ever seem dead, check
the log (captures/x1d-tether.log from a checkout, ~/Library/Logs/X1D Tether otherwise) for
"click ..." lines before changing any widget code. Space and Return also fire CAPTURE.

    python3 capture/viewer.py [--record out.h264]

  reader thread    0x84 -> H.264 payloads -> deque(maxlen=N), drops oldest
  writer thread    deque -> ffplay stdin (blocking; isolated from the reader)
  worker thread    camera commands - capture, exposure, download
  this thread      Tk: controls and status only

Camera I/O never happens on the UI thread.
"""
import collections
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import hbctl              # noqa: E402
from lib import liveview as ls     # noqa: E402
from lib import params             # noqa: E402
import widgets as ui                # noqa: E402
import gallery                      # noqa: E402
import scopes                       # noqa: E402

import usb.core                    # noqa: E402  must come after hbctl

QUEUE_MAX = 8
BTN_ORANGE = "#F57513"        # Hasselblad's release-button orange
RELEASE = 58                  # the release's size: the tiles' height, so the row is no taller

# Where captures are kept unless the user picks somewhere else. ~/Pictures is the macOS
# convention for this, and the folder is created on first run rather than assumed.
DEFAULT_CAPTURE_DIR = "~/Pictures/X1D Tether"

# how many replay commands may go unanswered before the session counts as failed. The
# shutter describe alone answers in several frames, so one or two misses are normal.
SESSION_TOLERANCE = 4

# Exposure controls: (label, value code, name-table code). The name table maps the
# camera's raw values to the text the body shows; ISO has none and is decoded instead.
CONTROLS = (("Aperture", 0x0008, 0x003e),
            ("Shutter", 0x0009, 0x003f),
            ("ISO", 0x000a, None),
            # exposure compensation. The body's wheel moves QuickExposureAdjust, not
            # EvAdjust (0x0007, which stays at 0); both take -5..+5 in thirds
            ("EV", 0x005c, None))
CODE_MODE = 0x0003
NAME_CODES = {0x003e: "Aperture", 0x003f: "Shutter"}   # the body's own text for a value
CODE_BATTERY = 0x002f
CODE_FOCUS = 0x0059        # FocusPositionPercent: 0.0 near .. 1.0 infinity, pushed live
CODE_LENS = 0x003d         # 1 while a lens is attached, 0 when it comes off
CODE_FOCAL = 0x002e        # focal length in mm, pushed as a zoom turns
CODE_LENS_NAME = 0x0078    # the lens's name; HC lenses on the XH converter send none
NO_FOCUS = "focus not reported by this lens"

# focus stack panel: the camera's own named scale, and practical frame counts
STACK_FRAMES = (2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30)
STEP_NAMES = ("Extra small", "Small", "Medium", "Large", "Extra large")
DIR_NAMES = ("Towards infinity", "Towards near", "Symmetric")


def _on_screen(root, geom):
    """A remembered "WxH+X+Y" or "+X+Y", or None if it would open off the screen (a
    display that has since been unplugged) or is not a geometry at all."""
    m = re.match(r"^(?:\d+x\d+)?\+(-?\d+)\+(-?\d+)$", geom or "")
    if not m:
        return None
    x, y = (int(v) for v in m.groups())
    if x < 0 or y < 0 or x + 100 > root.winfo_screenwidth() \
            or y + 100 > root.winfo_screenheight():
        return None
    return geom


def shutter_text(v):
    """Whole and decimal seconds get a unit ('4.0' -> '4.0s'); 1/125 and 2:30 read fine."""
    return v + "s" if re.match(r"^\d+(\.\d+)?$", v) else v


def aperture_text(v):
    """Aperture menu names are bare numbers; the tile shows them as photographers do."""
    return v if not v[:1].isdigit() else "f/" + v


# Exposure mode as reported by 0x0003. The body owns this and the host cannot set it
# (a write is refused with ack 0x0d), so these are only ever read.
# Mapped from the dial by observation - see analysis/README.md:
#   9 = full auto (square), 3 = P, 2 = S, 1 = A, 0 = M, 8 = Mq, 10 = video
# A C slot reports whatever mode it is programmed to (so a C set to A reads 1), which
# means the greying rules follow it automatically.
MODE_MQ = 8                 # M without live view
MODE_VIDEO = 10             # no tether preview at all

MODE_NAMES = {9: "\u25a1", 3: "P", 2: "S", 1: "A", 0: "M", 8: "Mq", 10: "Video"}

# badge tint by mode family: neutral where the camera decides, warmer as you take over
MODE_ACCENT = {9: "#455a64", 3: "#455a64",     # auto / P - camera chooses
               2: "#3949ab", 1: "#3949ab",     # S / A - one axis yours
               0: "#6a1b9a", 8: "#6a1b9a",     # M / Mq - both yours
               10: "#00695c"}                  # video

# which controls the mode lets you set: (aperture, shutter).
# C1-C3 are user-programmable, so they stay permissive - whatever they are set to
# reports that mode's own number anyway, so the value still drives the rule.
MODE_ALLOWS = {
    9:  (False, False),   # full auto - camera chooses both
    3:  (False, False),   # P - program, both shift together
    2:  (False, True),    # S - shutter only
    1:  (True, False),    # A - aperture only
    0:  (True, True),     # M
    8:  (True, True),     # Mq - M without live view
    10: (True, True),     # video - exposure is lens/menu dependent, stay permissive
}


def _mix(a, b, t):
    """Blend two #rrggbb colours by t (0 = a, 1 = b).

    Canvas fills have no alpha, so every shade on the release button has to be worked out
    against the background instead of being laid on top of it.
    """
    a = a.lstrip("#")
    b = b.lstrip("#")
    ca = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, int(round(x + (y - x) * t)))) for x, y in zip(ca, cb))


def status_text(now, mode, *, disconnected, live, session_ok, live_refused,
                refused_since, recover_until, last_frame):
    """The one-line connection state shown in the status row.

    Pure so it can be read (and tested) apart from the threads that feed it: `now` and
    the three timestamps are time.time() values, `mode` is the 0x0003 exposure mode.
    """
    if disconnected:
        return "camera disconnected - waiting"
    if mode == MODE_VIDEO and not live:
        # the release does nothing in video mode, and video cannot be recorded tethered
        return "video mode - no stills; set the dial to shoot"
    if not live:
        # no preview is deliberate here: the body shutter only works in a session that
        # never started live view
        if session_ok:
            return "remote release armed - press the body shutter"
        return "arming remote release (session) ..."
    if live_refused:
        held = now - refused_since if refused_since else 0.0
        if mode == MODE_MQ:
            return "Mq - no live view by design"
        if mode == MODE_VIDEO:
            # video mode does not tether a preview at all - Phocus cannot either
            return "video mode - no tether preview"
        if held > 20:
            return "refused for %ds - likely too hot, let it cool" % int(held)
        return "refused live view - wake the screen / exit menus"
    if now < recover_until:
        return "recovering (camera writing)"
    if now - last_frame > 6:
        if mode == MODE_MQ:
            return "no preview - Mq has no live view by design"
        return "no preview - check the camera body"
    return "live"


class App:
    def __init__(self, root, record=None):
        self.root = root
        self.running = True
        self.lock = threading.Lock()            # serialises camera commands
        self.payloads = collections.deque(maxlen=QUEUE_MAX)
        self.payloads_lock = threading.Lock()
        self.times = collections.deque(maxlen=128)   # >2 s at the full ~23 fps
        self.drops = 0
        self._frames_seen = 0
        self._last_frame_time = time.time()
        self._epoch = 0
        self._synced = False
        self.downloading = False
        self._seen_tokens = set()
        self._seen_ops = set()
        self.last_body_file = None
        self._live_refused = False
        self._mode = None
        self._refused_since = 0.0
        self._recover_until = 0.0
        self.capturing = False
        self.disconnected = True
        self.cam = None
        self.session_ok = False
        self._establishing = False              # a session replay is running
        self._live_switching = False            # a Live view toggle is being applied
        self._busy_shown = False                # Live view is greyed out for it
        self.player = None                      # ffplay, running only while live view is
        self._player_lock = threading.Lock()
        self._player_written = 0                # payloads sent to the current player
        self._pending_live_off = False          # preview closed while the camera was busy
        self.download_dir = None
        self._vars = {}                         # control label -> tk.StringVar
        self._menus = {}                        # control label -> ui.ValueTile
        self._values = {}                       # control label -> {shown text: raw value}
        self._codes = {label: code for label, code, _ in CONTROLS}
        self._applied = {}
        self._picked_at = {}                    # control label -> time of the last menu pick
        self._named = {}                        # control label -> mode when the body named it
        self._capture_enabled = True
        self._video_mode = False                # the dial is on video: no stills to take
        self._release_hovered = False
        self._cache_path = None
        self._last_pkt_log = 0.0
        self._cfg = os.path.expanduser("~/.x1d_viewer.json")
        self._log_path = self._make_log_path()
        self._logf = open(self._log_path, "a", buffering=1)
        self._log("--- viewer start ---")

        try:
            with open(self._cfg) as fh:
                cfg = json.load(fh)
            saved = cfg.get("download_dir")
            if saved and os.path.isdir(saved):
                self.download_dir = saved
            # live view is off by default: the camera will not fire from its own shutter
            # while the preview streams, and the cable release is the main workflow here
            self._live_pref = bool(cfg.get("live_view", False))
            self._save_pref_on = bool(cfg.get("save_copy", True))
            self._gallery_geom = _on_screen(root, cfg.get("gallery_geometry"))
            self._scopes_geom = _on_screen(root, cfg.get("scopes_geometry"))
            self._vec_zoom = int(cfg.get("vectorscope_zoom", 1))
        except Exception:
            self._live_pref = False
            self._save_pref_on = True
            self._gallery_geom = None
            self._scopes_geom = None
            self._vec_zoom = 1
        if not self.download_dir:
            self.download_dir = self._default_capture_dir()

        # ---- controls
        root.title("X1D II tether")
        root.configure(bg=ui.BG)
        root.columnconfigure(0, weight=1)

        # Kept as low as it will go, so the live view and the captures below it lose as
        # little of the screen as possible: one row of tiles, and a thin footer.
        bar = tk.Frame(root, bg=ui.BG)
        bar.grid(row=0, column=0, sticky="we", padx=14, pady=(12, 10))

        self._make_release(bar)

        # exposure: tiles reading like the camera's own top display
        for label, _, _ in CONTROLS:
            self._make_control(bar, label)

        # the lens, and where it is focused, live: the body pushes both as rings turn
        self.lens_tile = ui.LensTile(bar)
        self.lens_tile.pack(side="left", padx=(0, 8))

        # focus stack: one tile, its settings in its menu. When on, the release runs the
        # stack, so there is one primary action rather than two buttons.
        self.stack_on = tk.BooleanVar(value=False)
        self.stack_frames = tk.IntVar(value=3)
        self.stack_step = tk.StringVar(value=STEP_NAMES[0])
        self.stack_dir = tk.StringVar(value=DIR_NAMES[0])
        self.stack_tile = ui.StackTile(bar, self.stack_on, self.stack_frames,
                                       self.stack_step, self.stack_dir, STACK_FRAMES,
                                       STEP_NAMES, DIR_NAMES, command=self._toggle_stack)
        self.stack_tile.pack(side="left")

        # modes, on the right: two rows spanning the tiles' height, switches in one
        # column. Live view has to be a switch, not a nicety: while the preview streams
        # the body refuses to fire from its own shutter, and stopping the stream does not
        # bring it back - only a session that never started live view behaves. The
        # status line says which one is armed.
        side = tk.Frame(bar, bg=ui.BG)
        side.pack(side="right", padx=(18, 0))
        self.live_var = tk.BooleanVar(value=self._live_pref)
        self.live_check = ui.Switch(side, "Live view", self.live_var, self._toggle_live)
        self.live_check.grid(row=0, column=0, sticky="w", pady=(4, 6))
        self.save_var = tk.BooleanVar(value=self._save_pref_on)
        ui.Switch(side, "Save to", self.save_var, self._toggle_save).grid(
            row=1, column=0, sticky="w", pady=(6, 4))
        self.dir_label = ui.Chip(side, (os.path.basename(self.download_dir)
                                        if self.download_dir else "(card only)"),
                                 self._pick_dir, max_width=130)
        self.dir_label.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 4))

        self._rule(root, 1)

        # footer: what the app is doing on the left; on the right, the captures window,
        # the mode and the battery, ending on the same edge as the switches above
        row = tk.Frame(root, bg=ui.BG)
        row.grid(row=2, column=0, sticky="we", padx=14, pady=(6, 7))
        row.columnconfigure(0, weight=1)

        self.status = tk.Label(row, text="starting...", anchor="w", bg=ui.BG,
                               fg=ui.MUTED, font=ui.font(root, 11), width=60)
        self.status.grid(row=0, column=0, sticky="w")

        # closing the captures window only hides it; this brings it back (G toggles it
        # from any of the app's windows)
        self.captures_btn = ui.Chip(row, "Captures", self._open_captures, glyph="grid",
                                    max_width=104)
        self.captures_btn.set_enabled(False)
        self.captures_btn.grid(row=0, column=1, sticky="e", padx=(8, 14))

        # the exposure mode as the body's own display shows it: a letter, no colour
        self.mode_badge = ui.ModeBadge(row)
        self.mode_badge.grid(row=0, column=2, sticky="e", padx=(0, 12))

        batt_box = tk.Frame(row, bg=ui.BG)
        batt_box.grid(row=0, column=3, sticky="e")
        self.batt_canvas = tk.Canvas(batt_box, width=26, height=14, bg=ui.BG,
                                     highlightthickness=0, bd=0)
        self.batt_canvas.pack(side="left", padx=(0, 4))
        self.batt_label = tk.Label(batt_box, text="--%", bg=ui.BG, fg=ui.MUTED,
                                   font=ui.font(root, 11), width=4, anchor="e")
        self.batt_label.pack(side="left")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        # a killed client leaves the camera mid-session, and the next connection fails with
        # an endpoint overflow until the body is power cycled. Close the session on the
        # signals a supervisor (or a terminal) would normally send.
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: self.root.after(0, self.on_close))
            except Exception:
                pass
        # diagnostic: log every click that reaches the app, with the widget it hit, so a
        # dead button can be told apart from a click that never arrived
        root.bind_all("<Button-1>",
                      lambda e: self._log("click %r at %d,%d on %s"
                                          % (e.num, e.x_root, e.y_root,
                                             e.widget.__class__.__name__)),
                      add="+")

        # ---- video: ffplay owns the picture
        # a killed process leaves ffplay holding the last frame; sweep those first
        try:
            subprocess.run(["pkill", "-f", "ffplay.*X1D II live view"],
                           check=False, timeout=5)
            time.sleep(0.3)
        except Exception:
            pass
        # ffplay itself is started with live view (_ensure_player); only its place on
        # screen is worked out here, because that needs Tk and so the UI thread
        self._preview_geom = self._preview_geometry()
        # with live view off, captures land in a four-up gallery in the preview's place
        left, top, _ = self._preview_geom
        room = (root.winfo_screenwidth() - left - 20, root.winfo_screenheight() - top - 30)
        self.gallery = gallery.Gallery(root, (left, top) + room,
                                       on_capture=self.on_capture, log=self._log,
                                       geometry=self._gallery_geom,
                                       on_geometry=self._gallery_moved,
                                       bind_keys=self._bind_keys,
                                       scopes=scopes.Scopes(
                                           root, geometry=self._scopes_geom,
                                           on_geometry=self._scopes_moved, log=self._log,
                                           vec_zoom=self._vec_zoom,
                                           on_vec_zoom=self._vec_zoomed,
                                           bind_keys=self._bind_keys))
        self._bind_keys(root)              # the keymap acts on the gallery and scopes
        # launched from a terminal or Finder the app is not necessarily active, and Tk
        # 8.6.12 drops clicks until it is
        self.root.after(300, self._raise_controls)
        self.root.after(150, self._watch_busy)
        self._rec = open(record, "wb") if record else None

        # Workers start once mainloop() runs: they call into Tk, which raises from a
        # worker thread until the event loop exists. The first connection is made by
        # _reconnect, off the UI thread - see the locking rule on _start_workers.
        self.root.after(0, self._start_workers)

    def _start_workers(self):
        """Start the background threads.

        Locking rule: the UI thread never waits for self.lock (the camera lock); camera
        I/O from a button goes to a worker. Tk runs every call on the UI thread, so a
        worker that updates a widget while holding the lock waits for the UI thread; if
        the UI thread were itself waiting for the lock, both would hang for good. Keeping
        Tk calls outside the lock where it is easy (as _load_controls does) also stops a
        busy UI from holding up the camera.
        """
        for fn in (self._reader, self._writer, self._watchdog, self._reconnect,
                   self._stats, self._poll):
            threading.Thread(target=fn, daemon=True).start()

    # ---- ui helpers

    # ---- keyboard: the same keys in every window

    def _keymap(self):
        """Key -> action, for the controls, the gallery and the scopes alike.

        One map bound on every window, rather than each window binding what it knows
        about: the scopes window takes focus when it appears, and keys that only the
        gallery listened to then did nothing. With the same map everywhere, it does not
        matter which window has focus, and the app can be run from the keyboard and the
        camera's own controls without reaching for the mouse.
        """
        keys = {
            "<space>": self.on_capture,
            "<Return>": self.on_capture,
            "g": self.gallery.toggle,
            "s": self.gallery.scopes.toggle,
            "z": self.gallery.scopes.toggle_vec_zoom,
            "l": self._key_live,
            "f": self._key_stack,
            "<Escape>": self.gallery.show_grid,
            "<BackSpace>": self._remove_capture,     # the Mac's delete key
            "<Delete>": self._remove_capture,
        }
        for n in range(1, gallery.SLOTS + 1):
            keys[str(n)] = lambda i=n - 1: self._show_capture(i)
        return keys

    def _bind_keys(self, win):
        """Bind the keymap on `win` (a Toplevel or the root); letters both ways round,
        so Caps Lock or Shift does not turn them off."""
        for key, fn in self._keymap().items():
            for name in ({key} if key.startswith("<") else {key.lower(), key.upper()}):
                win.bind(name, lambda e, fn=fn: fn())

    def _remove_capture(self):
        """Backspace: drop a picture from the captures window. The .3FR is kept."""
        title = self.gallery.remove()
        if title:
            self._log("gallery: removed %s (file kept)" % title)
            self._say("%s removed from the window - the file is kept" % title)

    def _show_capture(self, index):
        """1-4: that capture on its own, opening the gallery if it was closed."""
        if self.gallery.open():
            self.gallery.show_single(index)

    def _key_live(self):
        """L: the Live view switch, with the same rule as a click - not while the
        camera is busy (the switch is greyed out then)."""
        if self.live_check.enabled:
            self.live_check.activate()

    def _key_stack(self):
        """F: the focus stack on or off."""
        self.stack_on.set(not self.stack_on.get())
        self._toggle_stack()

    def _rule(self, root, row):
        """A hairline between sections, inset to the content."""
        rule = tk.Frame(root, bg=ui.TILE_EDGE, height=1)
        rule.grid(row=row, column=0, sticky="we", padx=14)

    def _make_control(self, parent, label):
        # a drawn tile with the native menu: tk.OptionMenu cannot be styled on aqua, and
        # ttk.Combobox defers <<ComboboxSelected>> until focus moves, so picking a value
        # appeared to do nothing. A menu item's command fires on the click.
        var = tk.StringVar(value="")
        tile = ui.ValueTile(parent, label, var,
                            width={"Aperture": 116, "Shutter": 124}.get(label, 86),
                            command=lambda _v: self._set_param(label),
                            display={"Aperture": aperture_text,
                                     "Shutter": shutter_text}.get(label))
        tile.pack(side="left", padx=(0, 8))
        self._vars[label] = var
        self._menus[label] = tile
        self._values[label] = {}

    # ---- the release button

    def _make_release(self, parent):
        """The release, drawn rather than photographed.

        Modelled on the X release cord: a wide matte grey disc with a thin dark rim, a
        recessed well, and a small matte orange button sitting down inside it - not a
        glossy dome. tk.Button cannot be a circle on aqua (it paints its own rounded
        rectangle and ignores most styling), so this is a Canvas.
        """
        self.capture_canvas = tk.Canvas(parent, width=RELEASE, height=RELEASE,
                                        bg=ui.BG, highlightthickness=0, bd=0,
                                        cursor="hand2")
        self.capture_canvas.pack(side="left", padx=(0, 14))
        for w in (self.capture_canvas,):
            w.bind("<Button-1>", self._release_click)
            w.bind("<Enter>", lambda e: self._release_hover(True))
            w.bind("<Leave>", lambda e: self._release_hover(False))
        self._draw_release()

    def _release_click(self, _event=None):
        if not self._capture_enabled:
            return
        self.on_capture()

    def _release_hover(self, on):
        self._release_hovered = on
        self._draw_release()

    def _set_capture_enabled(self, enabled):
        """Dim the release while a shot or a stack is running. The geometry never
        changes, so the window cannot resize - that was what made the old button feel
        unreliable."""
        self._capture_enabled = bool(enabled)
        self._draw_release()

    def _draw_release(self):
        c = self.capture_canvas
        c.delete("all")
        on = self._capture_enabled and not self._video_mode
        c.configure(cursor="hand2" if on else "arrow")
        hot = self._release_hovered and on
        core = BTN_ORANGE if on else "#6b6b6b"
        if hot:
            core = _mix(core, "#ffffff", 0.10)
        # the same proportions as before, at the tiles' height (it was 70 with a caption)
        k = RELEASE / 70.0
        cx = cy = RELEASE / 2.0
        r_out, r_well, r_core = 33.0 * k, 21.5 * k, 17.0 * k
        # the plate never changes: only the button in the middle does, so "busy" reads as
        # the one part that is actually working being taken out of play
        grey_dark = "#767676"
        grey_lite = "#a8a8a8"

        # thin dark rim, then the wide matte face - a shallow ramp, not a glossy dome
        c.create_oval(cx - r_out - 1, cy - r_out - 1, cx + r_out + 1, cy + r_out + 1,
                      fill="#292929", outline="")
        steps = 26
        for i in range(steps):
            t = i / float(steps - 1)
            r = r_out * (1.0 - t) + 0.8
            off = 0.9 * (1.0 - (1.0 - t) ** 2)
            c.create_oval(cx - r - off, cy - r - off, cx + r - off, cy + r - off,
                          fill=_mix(grey_dark, grey_lite, t ** 0.9), outline="")

        # the recess: dark at the top left where the light does not reach, a lit wall at
        # the bottom right. This is what says "recessed" rather than "printed on".
        c.create_oval(cx - r_well, cy - r_well, cx + r_well, cy + r_well,
                      fill="#191919", outline="")
        c.create_arc(cx - r_well + 1, cy - r_well + 1, cx + r_well - 1, cy + r_well - 1,
                     start=95, extent=170, style="arc", outline="#0b0b0b", width=3)
        c.create_arc(cx - r_well + 1, cy - r_well + 1, cx + r_well - 1, cy + r_well - 1,
                     start=272, extent=150, style="arc", outline="#484848", width=2)

        # the matte button itself: low contrast, no dimple, a whisper of sheen
        edge = _mix(core, "#000000", 0.14)
        lite = _mix(core, "#ffffff", 0.15)
        for i in range(steps):
            t = i / float(steps - 1)
            r = r_core * (1.0 - t) + 0.6
            off = 1.4 * (1.0 - (1.0 - t) ** 2)
            c.create_oval(cx - r - off, cy - r - off, cx + r - off, cy + r - off,
                          fill=_mix(edge, lite, t ** 0.9), outline="")


    def _toggle_stack(self):
        """Enable the stack controls. Nothing is packed or unpacked and the capture
        button never changes its label, so the window cannot resize as a side effect."""
        on = self.stack_on.get()
        self._log("focus stack mode -> %s" % on)

    def _preview_geometry(self):
        """Where ffplay's window goes: (left, top, height) in screen pixels.

        Directly below the controls and left-aligned with them, sized to the screen space
        left underneath (ffplay keeps the aspect ratio when only the height is forced).
        """
        self.root.update_idletasks()
        self.root.update()                       # map the window so its position is real
        left = self.root.winfo_rootx()
        bottom = self.root.winfo_rooty() + self.root.winfo_height()
        top = bottom + 40                        # room for the preview's own title bar
        height = max(240, min(900, self.root.winfo_screenheight() - top - 40))
        return left, top, height

    def _raise_controls(self):
        """Make this the active app, as clicking its title bar does.

        Tk 8.6.12 drops mouse presses while the app is inactive, so after anything that
        takes activation away (launch from a terminal, ffplay's window appearing) the
        first click on a button would otherwise only activate the window.
        """
        try:
            self.root.lift()
            self.root.focus_force()
            self.root.focus_set()
        except Exception:
            pass

    # ---- saving

    def _default_capture_dir(self):
        """~/Pictures/X1D Tether, created if it is not there yet.

        Falls back to the home directory and then to None, so a read-only or missing
        ~/Pictures cannot stop the app from starting.
        """
        path = os.path.expanduser(DEFAULT_CAPTURE_DIR)
        for candidate in (path, os.path.expanduser("~")):
            try:
                os.makedirs(candidate, exist_ok=True)
                return candidate
            except Exception:
                continue
        return None

    def _pick_dir(self):
        d = filedialog.askdirectory(title="Where should captures be saved?",
                                    initialdir=self.download_dir or os.path.expanduser("~"))
        self.root.lift()
        self.root.focus_force()          # a modal dialogue leaves focus behind
        if d:
            self.download_dir = d
            self.dir_label.set_text(os.path.basename(d) or d)
            self._log("save dir = %s" % d)
            self._save_pref()
            if self.save_var.get():
                threading.Thread(target=self._ensure_session, daemon=True).start()

    def _toggle_save(self):
        on = self.save_var.get()
        self._log("save a copy -> %s" % on)
        self._save_pref()
        if on:
            if not self.download_dir:
                self.dir_label.set_text("(no folder chosen)")
                self._say("no folder chosen - click the folder next to Save to")
            threading.Thread(target=self._ensure_session, daemon=True).start()

    # ---- live view / cable release

    def _camera_busy(self):
        """True while the camera is in the middle of something a mode switch would break:
        a session replay, a capture or stack (until CAPTURE is live again), a download,
        or a previous Live view switch still being applied."""
        return (self._live_switching or self._establishing or self.capturing
                or self.downloading or not self._capture_enabled)

    def _watch_busy(self):
        """UI thread, every 150 ms: grey out Live view while the camera is busy.

        Polled rather than set at each busy point because those are spread over several
        worker threads, and a worker must not touch Tk itself (see _start_workers).
        """
        if not self.running:
            return
        self._check_player()
        busy = self._camera_busy()
        if busy != self._busy_shown:
            self._busy_shown = busy
            self.live_check.set_enabled(not busy)
        self.root.after(150, self._watch_busy)

    def _gallery_moved(self, geom):
        """The user moved or resized the captures window: open it there next time."""
        self._gallery_geom = geom
        self._save_pref()

    def _scopes_moved(self, geom):
        """The user moved the scopes window: open it there next time."""
        self._scopes_geom = geom
        self._save_pref()

    def _vec_zoomed(self, zoom):
        """The user toggled the vectorscope zoom: keep it for next time."""
        self._vec_zoom = zoom
        self._save_pref()

    def _save_pref(self):
        """Persist the choices that survive a restart."""
        cfg = {}
        try:
            with open(self._cfg) as fh:
                cfg = json.load(fh)
        except Exception:
            pass
        cfg["live_view"] = bool(self.live_var.get())
        cfg["save_copy"] = bool(self.save_var.get())
        if self._gallery_geom:
            cfg["gallery_geometry"] = self._gallery_geom
        if self._scopes_geom:
            cfg["scopes_geometry"] = self._scopes_geom
        cfg["vectorscope_zoom"] = self._vec_zoom
        if self.download_dir:
            cfg["download_dir"] = self.download_dir
        try:
            with open(self._cfg, "w") as fh:
                json.dump(cfg, fh)
        except Exception as e:
            self._log("saving preferences failed: %s" % e)

    # ---- the preview process

    def _ensure_player(self):
        """Start ffplay unless it is already running. Safe from any thread.

        A minimal probe rather than -fflags nobuffer: both keep start-up latency low, but
        with nobuffer ffplay opens its window before it knows the video size and then
        ignores -left/-top/-x/-y, landing at 640x480 in the middle of the screen.
        """
        with self._player_lock:
            if self.player is not None and self.player.poll() is None:
                return
            left, top, height = self._preview_geom
            try:
                self.player = subprocess.Popen(
                    ["ffplay", "-loglevel", "error", "-probesize", "32",
                     "-analyzeduration", "0", "-flags", "low_delay", "-framedrop",
                     "-autoexit", "-f", "h264",
                     "-left", str(left), "-top", str(top), "-y", str(height),
                     "-window_title", "X1D II live view", "-i", "-"],
                    stdin=subprocess.PIPE)
            except OSError as e:
                self.player = None
                self._log("preview: cannot start ffplay (%s)" % e)
                self._say("live view needs ffplay - brew install ffmpeg")
                return
            self._player_written = 0
            self._log("preview: ffplay started (pid %d)" % self.player.pid)

    def _stop_player(self, wait=1.5):
        """Close the preview window. Safe from any thread.

        Closing stdin is what ends ffplay cleanly: it plays to end-of-file and exits
        (-autoexit). It is killed if that takes longer than `wait`.
        """
        with self._player_lock:
            p, self.player = self.player, None
        if p is None:
            return
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(timeout=wait)
        except Exception:
            p.kill()
        self._log("preview: ffplay stopped")

    def _check_player(self):
        """UI thread: closing the preview window means "live view off".

        ffplay exiting on its own - its window was closed - is treated exactly like
        unticking Live view. If the camera is busy, the box is unticked straight away
        (so nothing restarts the preview) and the camera side follows once it is free.
        """
        p = self.player
        if p is not None and p.poll() is not None:
            with self._player_lock:
                if self.player is p:
                    self.player = None
            self._log("preview: window closed (ffplay exit %s)" % p.returncode)
            if self._live_pref:
                self.live_var.set(False)
                self._live_pref = False
                self._save_pref()
                self._pending_live_off = True
        if self._pending_live_off and not self._camera_busy():
            self._pending_live_off = False
            self._log("live view -> False (preview window closed)")
            self._apply_live(False)

    def _toggle_live(self):
        """Swap between previewing and arming the cable release.

        While the preview streams the body ignores its own shutter button and the cable
        release; stopping the stream brings it straight back, so this is a switch rather
        than a reconnect. Captures still need a tether session to be offered to the host,
        so turning the preview off makes sure one exists.
        """
        on = self.live_var.get()
        if self._camera_busy():
            # a click that landed before _watch_busy greyed the box out: undo it
            self.live_var.set(not on)
            self._log("live view -> %s ignored: camera busy" % on)
            return
        self._live_pref = bool(on)
        self._save_pref()
        self._pending_live_off = False
        self._log("live view -> %s" % on)
        self._apply_live(on)

    def _apply_live(self, on):
        """Carry out a live view switch on the camera, on a worker thread."""

        def run():                       # camera I/O stays off the UI thread
            try:
                if self.cam is None:
                    return
                if on:
                    try:
                        self._start_live()
                    except Exception as e:
                        self._log("live view on failed: %s" % e)
                    self._say("live view on - the body shutter cannot fire while streaming")
                    return
                try:
                    self._stop_live()
                except Exception as e:
                    self._log("live view off failed: %s" % e)
                self._stop_player()
                if not self.session_ok:
                    self._ensure_session()
                else:
                    self._say("live view off - use the remote release or the body shutter")
            finally:
                self._live_switching = False
        self._live_switching = True
        threading.Thread(target=run, daemon=True).start()

    def _stop_live(self):
        """Stop the preview stream and stand the watchdog down for this mode."""
        with self.lock:
            self.cam.stop_live()
        self._live_refused = False
        self._refused_since = 0.0
        self._epoch += 1                 # reader drops its half-parsed buffer
        self._log("live view stopped")

    def _ensure_session(self):
        """Replay the tether session if one is not established yet.

        Everything except the replay itself is inside the try: this runs on a worker
        thread, and an exception escaping it is invisible - which is exactly how a
        missing session file went unnoticed and left the app silently unable to transfer
        anything the body shot.
        """
        if self.session_ok or self.cam is None or self._establishing:
            return
        self._establishing = True
        self.downloading = True              # the replay reads 0x84: reader stand down
        try:
            self._say("establishing tether session ...")
            with self.lock:
                failed = self.cam.replay_session(live=self._live_pref) or []
                self.cam.flush_stream(2.0)
            # a couple of describes legitimately answer in several frames and land
            # outside the reply window; only a broadly silent replay means no session
            self.session_ok = len(failed) <= SESSION_TOLERANCE
            self._log("session established (live=%s), %d of %d commands unanswered"
                      % (self._live_pref, len(failed),
                         self.cam.last_session_sent))
            if not self.session_ok:
                self._say("session incomplete (%d commands) - capture may not transfer"
                          % len(failed))
            elif self._live_pref:
                self._say("session ready - captures will be saved and kept on the card")
            else:
                self._say("session ready - use the remote release or the body shutter")
        except Exception as e:
            self._log("session failed: %s" % e)
            self._say("session failed: %s" % e)
        finally:
            self.downloading = False
            self._establishing = False

    # ---- connection

    def _try_connect(self):
        try:
            cam = hbctl.Camera(timeout=1500)
        except SystemExit:
            self._log("connect: camera not present")
            self.disconnected = True
            return False
        except Exception as e:
            self._log("connect: open failed (%s)" % type(e).__name__)
            self.disconnected = True
            return False
        cam.on_event = self._on_event        # async events (capture, file written)
        with self.lock:
            old, self.cam = self.cam, cam
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        self._epoch += 1
        self._synced = False
        self.session_ok = False
        self._last_frame_time = time.time()
        if self._live_pref:
            try:
                self._start_live()
            except Exception as e:
                self._log("connect: live view failed (%s)" % type(e).__name__)
                self.disconnected = True
                return False
            self._log("connect: live view started")
        else:
            # no preview: the point of this mode is that the body shutter still works, so
            # the tether session is what we need instead; _reconnect establishes it
            self._log("connect: live view off, establishing cable-release session")
        self.disconnected = False
        self._recover_until = time.time() + 3
        return True

    def _reconnect(self):
        """Connect, and reconnect whenever the camera goes away.

        In cable-release mode the session comes before the exposure menus: it is what
        lets the body's own shutter deliver images, while reading the menus holds the
        camera for several seconds and would only delay it.
        """
        first = True
        while self.running:
            if not first:
                time.sleep(1.5)
            if self.running and self.disconnected:
                if not first:
                    self._log("reconnect: retrying")
                if self._try_connect():
                    if not first:
                        self._say("camera reconnected")
                    # with live view on as well: only inside a session does the body
                    # push the focus position (0x0059) as the ring turns
                    self._ensure_session()
                    self._load_controls()
            first = False

    def _start_live(self):
        self._ensure_player()
        with self.lock:
            self.cam.stop_live()
            time.sleep(0.4)
            r = self.cam.start_live()
        self._live_refused = bool(r and r[1] != b"\x00\x00\x00\x00")
        if self._live_refused:
            if not self._refused_since:
                self._refused_since = time.time()
            self._log("live view refused, ack=%s" % (r[1].hex(" ") if r else "-"))
        else:
            self._refused_since = 0.0
        self._epoch += 1
        return r

    def _mark_lost(self, why):
        if not self.disconnected:
            self._log("connection lost: %s" % why)
            self.disconnected = True
            self.root.after(0, lambda: self.lens_tile.set_focus(None))

    def _load_controls(self):
        """Read each exposure control's choices, then fill its menu on the UI thread.

        Runs on a worker. The lock is held only for one control's camera reads at a time
        and never while Tk is touched (see _start_workers).
        """
        for label, code, name_code in CONTROLS:
            cam = self.cam
            if cam is None:
                return
            try:
                with self.lock:
                    desc = cam.describe(code)
                    cur = cam.read(code)
                    names = (params.name_table(cam.describe(name_code))
                             if name_code else [])
            except Exception:
                continue
            if label == "ISO":
                order, values = params.iso_choices(desc)
            elif label == "EV":
                order, values = params.ev_choices(desc)
            else:
                order, values = params.named_choices(desc, names)
            self._log("%s: %d choices current=%r" % (label, len(order), cur))
            self.root.after(0, lambda lb=label, o=order, v=values, c=cur:
                            self._fill_menu(lb, o, v, c))
        self._read_lens()

    def _read_lens(self):
        """Worker: the lens's name, focal length and range, and the focus position,
        until the body pushes the next ones as the zoom or focus ring turns."""
        cam = self.cam
        if cam is None:
            return
        try:
            with self.lock:
                pos = params.focus_position(cam.read(CODE_FOCUS))
                mounted = cam.read(CODE_LENS)
                name = params.lens_name(cam.read(CODE_LENS_NAME))
                focal = params.focal_length(cam.read(CODE_FOCAL))
                span = params.focal_range(cam.describe(CODE_FOCAL))
        except Exception:
            return
        self._log("lens: mounted=%r name=%r focal=%r range=%r focus=%r"
                  % (mounted, name, focal, span, pos))

        def show():
            if mounted == 0:
                self.lens_tile.clear_lens()
                return
            self.lens_tile.set_lens(name, focal, span)
            # only lenses that autofocus on the X bodies report a position (an HC lens
            # on the XH converter may not); say so rather than leave a bare "--"
            self.lens_tile.set_focus(pos, None if pos is not None else NO_FOCUS)
        self.root.after(0, show)

    def _lens_attached(self):
        """UI thread: the body announced a newly attached lens (0x003d = 1).

        Not the lens name (0x0078): the HC 100mm on the XH Converter 0,8 attaches
        without sending one.

        After a lens change the body reports the focus position as unavailable (0x0059
        reads -1.0, and no events come as the ring turns) until the tether session is
        replayed. Waiting, re-describing the focus codes and restarting live view do not
        bring it back; a replay does. So replay it, unless one is already running or a
        stack has the session closed on purpose.
        """
        self._log("lens attached")
        if (self._establishing or self.capturing or self.cam is None
                or self.disconnected or not self.session_ok):
            return
        self.session_ok = False

        def run():
            self._ensure_session()
            self._read_lens()
        threading.Thread(target=run, daemon=True).start()

    def _fill_menu(self, label, order, values, current):
        """UI thread: replace a control's menu with freshly read choices."""
        tile = self._menus[label]
        var = self._vars[label]
        tile.set_choices(order)
        self._values[label] = values
        if order:
            var.set(params.current_choice(order, values, current))
            tile.set_enabled(True)
            self._sync_value(label, current)     # off the menu: show it, not entry one
        else:
            var.set("n/a")
            tile.set_enabled(False)
        self._applied[label] = var.get()

    def _draw_mode(self, name, _accent):
        self.mode_badge.set(name)

    def _draw_battery(self, pct):
        """Small battery glyph plus the number, so the row never reflows."""
        c = self.batt_canvas
        c.delete("all")
        x0, y0, x1, y1 = 1, 2, 24, 12
        c.create_rectangle(x0, y0, x1 - 3, y1, outline=ui.MUTED, width=1)
        c.create_rectangle(x1 - 2, y0 + 3, x1, y1 - 3, fill=ui.MUTED, outline="")
        if pct is None:
            self.batt_label.configure(text="--%")
            return
        pct = max(0, min(100, int(pct)))
        span = (x1 - 3 - x0 - 2)
        fill = max(1, int(span * pct / 100.0))
        colour = ui.TEXT if pct > 20 else ui.ACCENT      # orange only when it matters
        c.create_rectangle(x0 + 1, y0 + 1, x0 + 1 + fill, y1 - 1, fill=colour, outline="")
        self.batt_label.configure(text="%d%%" % pct)

    def _apply_camera_state(self, mode, values):
        """UI thread: mirror what the body reports - greying and the values themselves.

        The dials on the body change these without telling the host, so the tiles follow
        the periodic read rather than only the one made at connect.
        """
        self._apply_mode_state(mode, values["Aperture"], values["Shutter"])
        for label, raw in values.items():
            self._sync_value(label, raw)

    def _param_event(self, code, value):
        """UI thread: a parameter the body announced itself (event 0x42).

        The body pushes `<code u8><typed value>` whenever a setting changes - including
        the name of the aperture or shutter speed the meter has chosen, which is the only
        way to see it: in A or S mode a plain read of that parameter returns 7fffffff
        unless live view is running.
        """
        tile = self.lens_tile
        if code == CODE_FOCUS:
            pos = params.focus_position(value)
            # -1.0 follows 0x003d = 0 on a detach: keep the "no lens" it set
            tile.set_focus(pos, None if pos is not None else tile.note)
            return
        if code == CODE_FOCAL:
            tile.set_focal(params.focal_length(value))
            return
        if code == CODE_LENS_NAME:
            tile.name = params.lens_name(value)
            tile.redraw()
            return
        if code == CODE_LENS:
            if value == 1:
                self._lens_attached()
            elif value == 0:
                tile.clear_lens()
            return
        label = NAME_CODES.get(code)
        if label and isinstance(value, str) and value:
            if time.time() - self._picked_at.get(label, 0) < 3:
                return
            self._named[label] = self._mode       # valid until the mode changes
            if self._vars[label].get() != value:
                self._vars[label].set(value)
            return
        for label, c in self._codes.items():
            if c == code:
                self._sync_value(label, value)

    def _sync_value(self, label, raw):
        """Show the camera's current value, greyed out or not.

        A menu choice is shown by its menu name; a rational the body reports but the menu
        does not list is spelled out the body's way.
        The 7fffffff "not applicable" sentinel - what the X1D II reports for whichever of
        aperture and shutter the mode sets, unless live view runs - shows as "--", unless
        the body has named the metered value in an event since the mode last changed.
        None, after a failed read, leaves the tile as it was.
        """
        if raw is None:
            return
        if str(raw) == "7fffffff":
            if self._named.get(label, -1) == self._mode:
                return                  # the body named the metered value; keep it
            if self._vars[label].get():
                self._vars[label].set("")          # the tile draws an empty value as --
            return
        if time.time() - self._picked_at.get(label, 0) < 3:
            return                      # the read may predate the user's own change
        text = next((t for t, v in self._values.get(label, {}).items()
                     if params.same_value(v, raw)), None)
        if text is None and label in ("Aperture", "Shutter"):
            text = params.rational_text(raw)      # e.g. the metered speed in A mode
        if text is None or self._vars[label].get() == text:
            return
        self._log("%s changed on the body: %s (%r)" % (label, text, raw))
        self._vars[label].set(text)
        self._applied[label] = text

    def _apply_mode_state(self, mode, aperture, shutter):
        """Grey out whatever the current exposure mode does not allow.

        The mode cannot be set over USB (the write is refused with ack 0x0d) - the body
        owns it - so this only mirrors what the camera reports.
        """
        video = mode == MODE_VIDEO
        if video != self._video_mode:
            self._video_mode = video
            self._log("release %s: video mode" % ("disabled" if video else "enabled"))
            self._draw_release()
        allow_ap, allow_sh = MODE_ALLOWS.get(mode, (True, True))
        # belt and braces: an inapplicable control also reads the 7fffffff sentinel
        allow_ap = allow_ap and aperture is not None and str(aperture) != "7fffffff"
        allow_sh = allow_sh and shutter is not None and str(shutter) != "7fffffff"
        self._set_enabled("Aperture", allow_ap)
        self._set_enabled("Shutter", allow_sh)

    def _set_enabled(self, label, enabled):
        tile = self._menus[label]
        if tile.enabled == bool(enabled):
            return
        tile.set_enabled(enabled)
        self._log("%s %s" % (label, "enabled" if enabled else "greyed out"))

    def _set_param(self, label):
        code = self._codes[label]
        values = self._values[label]
        text = self._vars[label].get()
        if text not in values:
            return
        raw = values[text]
        self._log("apply %s = %r (%r)" % (label, text, raw))
        self._applied[label] = text
        self._picked_at[label] = time.time()
        self._say("%s: setting %s ..." % (label, text))

        def run():
            try:
                with self.lock:
                    ok, before, after, ack = self.cam.write(code, raw)
                self._say("%s %s: %r -> %r%s" % (label, text, before, after,
                                                 "" if ok else "   (NOT APPLIED)"))
            except Exception as e:
                self._say("%s failed: %s" % (label, e))
        threading.Thread(target=run, daemon=True).start()

    # ---- capture

    def on_capture(self):
        if not self._capture_enabled:
            return                   # a shot or a stack is already running
        if self._video_mode:
            # In video mode the body ignores the release: no image is offered and nothing
            # is written, so waiting for one only held the camera for 20 s. The app
            # cannot record video either (Phocus cannot); the dial has to change.
            self._say("video mode - set the dial to a stills mode to shoot")
            return
        if self.stack_on.get():
            self.on_stack()          # the panel is open, so CAPTURE means "run the stack"
            return
        self._log("capture: click received")
        self._set_capture_enabled(False)
        self.root.update_idletasks()

        def run():
            self.capturing = True
            token = None
            in_session = False
            try:
                if self.save_var.get() and not self.download_dir:
                    self._say("no folder chosen - this shot goes to the card only")
                if self.save_var.get() and self.download_dir and not self.session_ok:
                    self._ensure_session()       # saving needs one; no-op if running
                # whether a session exists decides how the camera delivers the shot, so
                # wait for one that is being set up rather than racing it
                deadline = time.time() + 90
                while self._establishing and self.running and time.time() < deadline:
                    time.sleep(0.2)
                in_session = self.session_ok
                if in_session:
                    # In a session the camera offers every shot to the host and holds it
                    # until it is fetched - the same as a press of the body's shutter.
                    # release() would answer the offer with Phocus's commit frames, and
                    # the first of those starts a transfer that nothing then reads,
                    # leaving the camera busy (ack 09) until it is power cycled.
                    with self.lock:
                        token = self.cam.capture_token()
                    self._log("capture: tether token=%r" % token)
                else:
                    with self.lock:
                        token, acks = self.cam.release(timeout=12)
                    self._log("capture: token=%r acks=%s" % (token, acks))
            except Exception as e:
                self._log("capture: EXCEPTION %s: %s" % (type(e).__name__, e))
            finally:
                self.capturing = False
            if token is not None and in_session:
                self._seen_tokens.add(token)     # do not fetch it again from the event
                self._fetch_external(token)      # saves or caches it, shows the still
            self._recover_until = time.time() + 8
            time.sleep(1.2)
            if self._live_pref:
                try:
                    self._start_live()
                except Exception as e:
                    self._log("capture: restart failed %s" % e)
            self.root.after(0, lambda: self._set_capture_enabled(True))
            if token is None:
                self._say("capture failed - the camera did not report a shot")
            elif not in_session:
                self._say("captured (token 0x%04x)" % token)
        threading.Thread(target=run, daemon=True).start()

    def on_stack(self):
        """Run the camera's focus bracketing. Live view must be off for the sequence."""
        if self.capturing or self.cam is None or self.disconnected:
            return
        frames = int(self.stack_frames.get())
        step = STEP_NAMES.index(self.stack_step.get())
        self.capturing = True
        self._log("stack: start %d frames, step %s, %s"
                  % (frames, self.stack_step.get(), self.stack_dir.get()))
        self._set_capture_enabled(False)

        def run():
            got = []
            reopen = False
            try:
                self._say("stack: stopping preview\u2026")
                # Stop live view and CONFIRM the stream has stopped. With the preview
                # running the camera accepts the trigger but ignores the drive mode, so a
                # bracket silently becomes one frame - which is exactly what happened.
                for attempt in range(4):
                    with self.lock:
                        self.cam.stop_live()
                    quiet = time.time()
                    stopped = False
                    while time.time() - quiet < 1.5:
                        if self._last_frame_time > quiet:
                            break                 # a frame arrived: still streaming
                        time.sleep(0.1)
                    else:
                        stopped = True
                    self._log("stack: stop preview attempt %d, stopped=%s"
                              % (attempt + 1, stopped))
                    if stopped:
                        break
                self.downloading = True          # the reader must not take 0x84
                time.sleep(0.6)
                self._say("stack: starting\u2026")
                direction = DIR_NAMES.index(self.stack_dir.get())
                # the body runs a bracket only with no tether session open: in one it
                # offers the first frame to the host and stops there. So the session is
                # closed for the stack and reopened after it; the frames go to the card.
                with self.lock:
                    if self.session_ok:
                        self._say("stack: closing tether session\u2026")
                        self.cam.end_session()
                        self.session_ok = False
                        reopen = True
                        time.sleep(1.0)
                    got = self.cam.focus_bracket(
                        frames=frames, step=step, direction=direction,
                        timeout=30 + frames * 8,
                        progress=lambda p, n: self._say("stack %d/%d" % (n, frames)))
                self._log("stack: %d/%d frames" % (len(got), frames))
                if not got:
                    self._say("stack: no frames - camera busy, try again")
                elif len(got) >= frames:
                    self._say("stack: %d frames on card" % len(got))
                else:
                    self._say("stack: %d/%d - lens limit reached?" % (len(got), frames))
            except Exception as e:
                self._log("stack: EXCEPTION %s: %s" % (type(e).__name__, e))
                self._say("stack failed - %s" % type(e).__name__)
            finally:
                self.downloading = False
                self.capturing = False
                self._recover_until = time.time() + 10
                # a stack runs with live view off; only restore the preview if that is
                # the mode the user actually wants
                if self._live_pref:
                    try:
                        self._start_live()
                    except Exception as e:
                        self._log("stack: live restart failed %s" % e)
                    if reopen:
                        self._settle_card()
                        self._log("stack: reopening tether session")
                        self._ensure_session()     # the focus readout needs it
                elif reopen:
                    self._settle_card()
                    self._log("stack: reopening tether session")
                    self._ensure_session()     # body-shutter shots reach the app again
                self.root.after(0, lambda: self._set_capture_enabled(True))
        threading.Thread(target=run, daemon=True).start()
        # a wedged camera can block a USB read indefinitely; do not let that hold the UI
        self.root.after(int((frames * 10 + 45) * 1000), self._stack_watchdog)

    def _settle_card(self, quiet=2.0, limit=15.0):
        """Wait until the body has finished writing the stack before talking to it again.

        The stack counts frames on the first card, but the body also writes each frame to
        the second one; its last "file written" event arrives after the run is counted.
        Replaying the tether session in the middle of that write made the camera show an
        error, so wait for the control endpoint to stay quiet first.
        """
        end = time.time() + limit
        last = time.time()
        with self.lock:
            while time.time() < end and time.time() - last < quiet:
                p = self.cam._read_ctrl(300)
                if p is not None:
                    last = time.time()
                    if len(p) >= 3:
                        self._log("stack: settling, event 0x%02x" % p[2])
        self._log("stack: card settled after %.1f s" % (time.time() - (end - limit)))

    def _stack_watchdog(self):
        if not self.capturing:
            return
        self._log("stack: watchdog - camera stopped answering")
        self._say("stack timed out - camera stopped responding")
        self.capturing = False
        self.downloading = False
        self._set_capture_enabled(True)

    # ---- pipeline

    def _reader(self):
        buf = b""
        epoch = -1
        while self.running:
            if epoch != self._epoch:
                epoch = self._epoch
                buf = b""
                self._synced = False
            if self.cam is None or self.downloading:
                time.sleep(0.05)      # a download owns 0x84 while it runs
                continue
            try:
                chunk = bytes(self.cam.dev.read(hbctl.EP_DATA, 1 << 18, timeout=80))
                buf += chunk
            except usb.core.USBTimeoutError:
                continue
            except Exception as e:
                self._mark_lost("reader: %s" % type(e).__name__)
                time.sleep(0.5)
                continue
            frames, events, buf = ls.stream_parse(buf)
            for payload in events:
                self._on_event(payload)
            for frame in frames:
                payload = ls.payload(frame)
                if not self._synced:
                    if b"\x00\x00\x01\x67" not in payload:
                        continue
                    self._synced = True
                with self.payloads_lock:
                    if len(self.payloads) == self.payloads.maxlen:
                        self.drops += 1
                    self.payloads.append(payload)

    def _on_event(self, payload):
        """React to a capture made with the camera's own shutter or cable release.

        The body announces it two ways, and both show up in practice:

          0x45  <01 00> <dirId u32> "<path>"   the file the body just wrote to the card
          0x40  04 <id u32> ... "3FR"          the image offered to the host for transfer

        Only the 0x40 id can be handed to download(); 0x45 is the useful part for naming
        and for telling "the shutter fired" apart from "the host is asleep". Both are
        pushed only while a tether session is active, so the session must be established
        even when nothing is being saved.
        """
        if len(payload) < 3:
            return
        op = payload[2]
        body = payload[3:]
        if op not in self._seen_ops:
            self._seen_ops.add(op)
            self._log("event opcode 0x%02x len=%d: %s" % (op, len(payload), body.hex(' ')))

        if op == 0x42 and len(body) >= 2:
            code = body[0]
            _, value = hbctl.decode_typed(bytes(body[1:]))
            self._log("param event 0x%02x = %r" % (code, value))
            self.root.after(0, lambda c=code, v=value: self._param_event(c, v))
            return

        if op == 0x45:
            text = body.decode("latin-1", "replace")
            m = re.search(r"(/DCIM/\S+\.\w+)$", text)
            self.last_body_file = m.group(1) if m else None
            self._log("file event: %s (dir id %s)"
                      % (text, body[2:6].hex(' ') if len(body) >= 6 else "-"))
            return

        if op != 0x40 or len(body) < 5 or body[0] != 0x04:
            return
        token = int.from_bytes(body[1:5], "little")
        self._log("capture offered: id=0x%04x" % token)
        if token in self._seen_tokens:
            return
        self._seen_tokens.add(token)
        threading.Thread(target=self._fetch_external, args=(token,),
                         daemon=True).start()

    def _cache_dir(self):
        """Working files go to a throwaway directory, never a bare name in /tmp.

        A transfer is over 100 MB and the embedded JPEG another 45 MB, so where this
        lands matters. ~/Library/Caches is the macOS location for data that may be
        deleted at any time, which is exactly what these are.
        """
        if self._cache_path:
            return self._cache_path
        for d in (os.path.expanduser("~/Library/Caches/X1D Tether"),
                  os.path.expanduser("~/Library/Logs/X1D Tether"),
                  tempfile.gettempdir()):
            try:
                os.makedirs(d, exist_ok=True)
                self._cache_path = d
                return d
            except Exception:
                continue
        self._cache_path = tempfile.gettempdir()
        return self._cache_path

    def _fetch_external(self, token):
        """Pull an offered capture to the host and show it.

        Used for the body's shutter and the on-screen CAPTURE alike whenever a tether
        session is active.

        With Save a copy off the transfer is only needed to make the still, so the raw
        file is deleted once the JPEG is out of it - otherwise every shot would leave
        another 100 MB behind for a picture the card already holds.
        """
        try:
            self.downloading = True          # the reader must release 0x84
            time.sleep(0.4)
            with self.lock:
                self._store_capture(token)
        except Exception as e:
            self._log("external capture failed %s" % e)
            self._say("camera shot download failed - %s" % type(e).__name__)
        finally:
            self.downloading = False
            self._recover_until = time.time() + 8

    def _store_capture(self, token):
        """Download an offered capture, show it, and keep or drop the raw file.

        The caller holds self.lock and has the reader stood down (self.downloading).
        """
        saving = bool(self.save_var.get() and self.download_dir)
        if saving:
            dest = os.path.join(self.download_dir, "capture_%04x.3fr" % token)
        else:
            dest = os.path.join(self._cache_dir(), "capture_%04x.3fr" % token)
        self._say("camera shot 0x%04x - downloading ..." % token)
        n = self.cam.download(token, dest)
        self._log("external capture %s (%d bytes, expected %r)"
                  % (dest, n, self.cam.last_download_expected))
        if not n:
            self._say("camera shot 0x%04x - transfer refused" % token)
            return
        self._show_still(dest)
        if saving:
            self._say("saved %s" % os.path.basename(dest))
        else:
            self._say("camera shot 0x%04x (card only)" % token)
            try:
                os.remove(dest)          # the still is already extracted
            except Exception:
                pass

    def _show_still(self, raw_path):
        """Put a captured frame into the four-up gallery: the JPEG embedded in the 3FR.

        The same route with live view on or off; with it on, the gallery is a window of
        its own beside the H.264 player, so the preview needs no restart. Runs on a
        worker (decoding takes a moment).
        """
        try:
            with open(raw_path, "rb") as fh:
                data = fh.read(48 << 20)
        except Exception as e:
            self._log("still: cannot read %s (%s)" % (raw_path, e))
            return
        i = data.find(b"\xff\xd8\xff")
        j = data.rfind(b"\xff\xd9")
        if i < 0 or j <= i:
            self._log("still: no embedded JPEG in %s" % raw_path)
            return
        stem = os.path.splitext(os.path.basename(raw_path))[0]
        try:
            decoded = gallery.decode(data[i:j + 2])
        except Exception as e:
            self._log("still: cannot decode %s (%s)" % (stem, e))
            return
        self._log("still: %d bytes of JPEG from %s -> gallery" % (j - i + 2, stem))
        self.root.after(0, lambda: self._add_capture(decoded, stem))

    def _add_capture(self, decoded, stem):
        """UI thread: a new capture into the gallery, and the Captures button on."""
        self.gallery.add(decoded, stem)
        self.captures_btn.set_enabled(True)

    def _open_captures(self):
        """The Captures button or G: reopen the gallery the user closed."""
        self.gallery.open()

    def _poll(self):
        """Keep the camera's session warm the way Phocus does.

        Phocus polls indexed state (op 4) about once a second even when nothing changes.
        Without this the camera does not push camera-initiated capture events to us, even
        though it does to Phocus. The replies are empty - it is a keepalive, not a query.
        """
        while self.running:
            time.sleep(0.9)
            if self.cam is None or self.downloading or self.disconnected:
                continue
            try:
                with self.lock:
                    self.cam.call(hbctl.OP_INDEXED, 0x0003, b"", retries=0)
                    self.cam.call(hbctl.OP_INDEXED, 0x0002, b"", retries=0)
                    for v in (b"\x07\x00\x00\x00", b"\x08\x00\x00\x00"):
                        self.cam.call(hbctl.OP_INDEXED, 0x0001, v, retries=0)
            except Exception:
                pass

    def _writer(self):
        """Feed H.264 payloads to ffplay.

        While nothing is arriving, a one-byte-payload access unit delimiter - a no-op to
        the decoder - goes out once a second. ffplay's reader blocks in read() on this
        pipe, and closing its window waits for that reader: with no data, the window
        would hang (beachball) instead of closing.
        """
        keepalive = b"\x00\x00\x00\x01\x09\xf0"
        last_write = time.time()
        while self.running:
            with self.payloads_lock:
                payload = self.payloads.popleft() if self.payloads else None
            player = self.player
            if payload is None:
                if (player is not None and self._player_written
                        and time.time() - last_write > 1.0):
                    self._write_player(player, keepalive)
                    last_write = time.time()
                time.sleep(0.002)
                continue
            if self._rec:
                self._rec.write(payload)
            self._frames_seen += 1
            self.times.append(time.time())
            self._last_frame_time = time.time()
            if player is None:
                continue                 # preview closed; the frame is not shown
            if self._write_player(player, payload):
                last_write = time.time()
                self._player_written += 1
                if self._player_written == 1:
                    # ffplay maps its window on the first decoded frame and activates
                    # itself; take activation back once it has, or the next click is lost
                    for delay in (800, 2000):
                        self.root.after(delay, self._raise_controls)

    def _write_player(self, player, data):
        """Write to ffplay; False if it has gone (its window was closed)."""
        try:
            player.stdin.write(data)
            player.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False                 # _check_player notices the exit and reacts

    def _watchdog(self):
        while self.running:
            time.sleep(3)
            if not self.running or self.capturing or self.disconnected:
                continue
            if not self._live_pref:
                continue          # no preview is the point in cable-release mode
            if time.time() < self._recover_until:
                continue
            if time.time() - self._last_frame_time < 8:
                continue
            if self._mode in (MODE_MQ, MODE_VIDEO):
                continue          # Mq and video have no preview to restart
            self._log("watchdog: no frames for >8s, restarting live view")
            try:
                r = self._start_live()
                self._log("watchdog: restart ack=%s"
                          % (r[1].hex(" ") if r else "-"))
            except Exception as e:
                self._log("watchdog: %s" % e)
            self._last_frame_time = time.time()

    # ---- status

    def _stats(self):
        while self.running:
            time.sleep(2)
            # read the camera first: the state text depends on the exposure mode
            batt = mode = None
            try:
                if self.cam is not None:
                    with self.lock:
                        batt = self.cam.read(CODE_BATTERY)
                        now = {label: self.cam.read(code) for label, code, _ in CONTROLS}
                        mode = self.cam.read(CODE_MODE)
                        self._mode = mode
                    self.root.after(0, lambda m=mode, v=now: self._apply_camera_state(m, v))
            except Exception as e:
                self._mark_lost("stats: %s" % type(e).__name__)

            now = time.time()
            fps = sum(1 for t in self.times if t >= now - 2.0) / 2.0
            state = status_text(
                now, mode,
                disconnected=self.disconnected, live=self._live_pref,
                session_ok=self.session_ok, live_refused=self._live_refused,
                refused_since=self._refused_since, recover_until=self._recover_until,
                last_frame=self._last_frame_time)

            cam = self.cam
            if cam is not None and time.time() - self._last_pkt_log > 20:
                self._last_pkt_log = time.time()
                self._log("packets: %s"
                          % {hex(k): v for k, v in cam.pkt_stats.items()})
            text = state if not self._live_pref else "%.1f fps   %s" % (fps, state)
            name = MODE_NAMES.get(mode, "?")
            accent = MODE_ACCENT.get(mode, "#2b2b2b")
            self.root.after(0, lambda t=text: self.status.configure(text=t))
            self.root.after(0, lambda n=name, a=accent: self._draw_mode(n, a))
            self.root.after(0, lambda b=batt: self._draw_battery(b))
            self._log("fps=%.1f frames=%d drops=%d state=%s mode=%r"
                      % (fps, self._frames_seen, self.drops, state, mode))

    def _say(self, text):
        """Update the status line from any thread.

        Tk raises "main thread is not in main loop" if this is called before mainloop()
        is running, which happens for anything started during __init__. That exception
        used to escape a worker thread and kill it with no trace, so it is swallowed
        here: a status line is never worth losing a session over.
        """
        try:
            self.root.after(0, lambda: self.status.configure(text=text))
        except Exception:
            pass

    def _make_log_path(self):
        """Where to write the event log.

        Two rules, both learned by looking at where it actually ended up:

        * never inside the app bundle. `here` resolves to Contents/Resources/app when
          frozen, so the old first choice dropped the log into the .app itself - which
          pollutes the signed bundle and disappears on every rebuild.
        * never a bare guess in /tmp unless nothing else works.

        Running from a checkout keeps the log next to the captures it describes; the
        packaged app uses ~/Library/Logs, which is where macOS expects it.
        """
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates = []
        if os.path.isdir(os.path.join(here, "analysis")):
            candidates.append(os.path.join(here, "captures"))      # a checkout, not a bundle
        candidates.append(os.path.expanduser("~/Library/Logs/X1D Tether"))
        candidates.append(tempfile.gettempdir())
        for d in candidates:
            try:
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, "x1d-tether.log")
                with open(path, "a"):
                    pass
                return path
            except Exception:
                continue
        return os.path.join(tempfile.gettempdir(), "x1d-tether.log")

    def _log(self, msg):
        try:
            self._logf.write("%.3f %s\n" % (time.time(), msg))
        except Exception:
            pass

    def on_close(self):
        """Close the session cleanly if the camera is free, without ever hanging the UI.

        A worker may hold the lock for a long transfer. Waiting a few seconds is fine;
        waiting forever is not, and closing USB under a running transfer is worse than
        skipping the goodbye, so after the timeout the camera is simply left alone.
        """
        self.running = False
        if self._rec:
            self._rec.close()
        self._stop_player(wait=0.5)
        cam = self.cam
        if cam is not None:
            if self.lock.acquire(timeout=3.0):
                try:
                    cam.stop_live()
                    cam.close()
                except Exception:
                    pass
                finally:
                    self.lock.release()
            else:
                self._log("close: camera busy, leaving the session open")
        self.root.destroy()


def main():
    record = None
    if "--record" in sys.argv:
        record = sys.argv[sys.argv.index("--record") + 1]
    root = tk.Tk()
    App(root, record)
    root.mainloop()


if __name__ == "__main__":
    main()
