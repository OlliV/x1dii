#!/usr/bin/env python3
"""Tethering control window for the Hasselblad X1D II.

Video goes to ffplay in its own window; this window is only the controls. That split is
deliberate: Tk's photo-image path costs real compositing time per frame and made the
buttons feel dead, while ffplay is a native video renderer and stays smooth.

Because ffplay is a separate process, the control window is forced topmost. Without it the
preview can sit over the controls and swallow every mouse click, which is indistinguishable
from an unresponsive UI - the clicks never reach this process at all. If buttons ever seem
dead, check the log (captures/x1d-tether.log from a checkout, ~/Library/Logs/X1D Tether
otherwise) for "click ..." lines before changing any widget code.

On macOS the window can be visible and topmost yet not actually be the key window; then
clicks never reach the process. Clicking the title bar fixes it, and _raise_controls() does
the equivalent automatically at startup. Space and Return also fire CAPTURE, so the app
stays usable from the keyboard.

    python3 capture/viewer.py [--record out.h264]

  reader thread    0x84 -> H.264 payloads -> deque(maxlen=N), drops oldest
  writer thread    deque -> ffplay stdin (blocking; isolated from the reader)
  worker thread    camera commands - capture, exposure, download
  this thread      Tk: controls and status only

Camera I/O never happens on the UI thread.
"""
import collections
import os
import re
import struct
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import hbctl              # noqa: E402
from lib import liveview as ls     # noqa: E402

import usb.core                    # noqa: E402  must come after hbctl

QUEUE_MAX = 8
BTN_ORANGE = "#F57513"        # Hasselblad's release-button orange

# Where captures are kept unless the user picks somewhere else. ~/Pictures is the macOS
# convention for this, and the folder is created on first run rather than assumed.
DEFAULT_CAPTURE_DIR = "~/Pictures/X1D Tether"

# how many replay commands may go unanswered before the session counts as failed. The
# shutter describe alone answers in several frames, so one or two misses are normal.
SESSION_TOLERANCE = 4

# Exposure mode as reported by 0x0003. The body owns this and the host cannot set it
# (a write is refused with ack 0x0d), so these are only ever read.
# Mapped from the dial by observation - see analysis/README.md:
#   9 = full auto (square), 3 = P, 2 = S, 1 = A, 0 = M, 8 = Mq, 10 = video
# A C slot reports whatever mode it is programmed to (so a C set to A reads 1), which
# means the greying rules follow it automatically.
# focus stack panel: the camera's own named scale, and practical frame counts
STACK_FRAMES = (2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30)
STEP_NAMES = ("Extra small", "Small", "Medium", "Large", "Extra large")
DIR_NAMES = ("Towards infinity", "Towards near", "Symmetric")

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


def descriptor_list(desc):
    """'LR11,a,b,...' -> ['a','b',...]"""
    if not desc:
        return []
    m = re.match(r"^L[A-Za-z](\d+),(.*)$", desc)
    return m.group(2).split(",") if m else []


def name_table(desc):
    """'LS34,4\\t4.8' -> ['4','4.8']  (the leading number is string length)"""
    if not desc:
        return []
    m = re.match(r"^LS\d+,(.*)$", desc)
    return m.group(1).split("\t") if m else []


def rational(name):
    """Shutter/aperture values are rationals: (numerator << 16) | denominator."""
    try:
        if "/" in name:
            a, b = name.split("/")
            return (int(a) << 16) | int(b)
        if ":" in name:
            a, b = name.split(":")
            return ((int(a) * 100 + int(b)) << 16) | 100
        if "." in name:
            whole, frac = name.split(".")
            num = int(whole) * 10 ** len(frac) + int(frac)
            return (num << 16) | 10 ** len(frac)
        return (int(name) << 16) | 1
    except Exception:
        return None


def _same(a, b):
    if isinstance(a, str) and isinstance(b, str) and a == b:
        return True
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return str(a) == str(b)


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


class App:
    def __init__(self, root, record=None):
        self.root = root
        self.running = True
        self.lock = threading.Lock()            # serialises camera commands
        self.payloads = collections.deque(maxlen=QUEUE_MAX)
        self.payloads_lock = threading.Lock()
        self.times = collections.deque(maxlen=32)
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
        self.download_dir = None
        self._saved_path = None
        self._combo_code = {"Aperture": 0x0008, "Shutter": 0x0009, "ISO": 0x000a}
        self._applied = {}
        self._cfg = os.path.expanduser("~/.x1d_viewer.json")
        self._log_path = self._make_log_path()
        self._logf = open(self._log_path, "a", buffering=1)
        self._log("--- viewer start ---")

        try:
            import json
            cfg = json.load(open(self._cfg))
            saved = cfg.get("download_dir")
            if saved and os.path.isdir(saved):
                self.download_dir = saved
            # live view is off by default: the camera will not fire from its own shutter
            # while the preview streams, and the cable release is the main workflow here
            self._live_pref = bool(cfg.get("live_view", False))
            self._save_pref_on = bool(cfg.get("save_copy", True))
        except Exception:
            self._live_pref = False
            self._save_pref_on = True
        if not self.download_dir:
            self.download_dir = self._default_capture_dir()

        # ---- controls
        root.title("X1D II tether")
        root.configure(bg="#111")

        bar = tk.Frame(root, bg="#111")
        bar.grid(row=0, column=0, sticky="we", padx=8, pady=8)

        self._make_release(bar)

        self._make_control(bar, "Aperture", 0x0008, 0x003e)
        self._make_control(bar, "Shutter", 0x0009, 0x003f)
        self._make_control(bar, "ISO", 0x000a, None)

        save = tk.Frame(bar, bg="#111")
        save.pack(side="left", padx=(18, 4))
        self.save_var = tk.BooleanVar(value=self._save_pref_on)
        tk.Checkbutton(save, text=" Save a copy", variable=self.save_var,
                       bg="#111", fg="#ccc", selectcolor="#4a4a4a",
                       activebackground="#111", activeforeground="#fff",
                       highlightthickness=0, font=("Helvetica", 11),
                       command=self._toggle_save).pack(anchor="w")
        self._flat_button(save, "folder…", self._pick_dir).pack(anchor="w", pady=(2, 0))
        self.dir_label = tk.Label(save, bg="#111", fg="#777", font=("Menlo", 9),
                                  text=(os.path.basename(self.download_dir)
                                        if self.download_dir else "(card only)"))
        self.dir_label.pack(anchor="w")

        # live view has to be a switch, not a nicety: while the preview streams the body
        # refuses to fire from its own shutter, and stopping the stream does not bring it
        # back - only a session that never started live view behaves. So the two modes are
        # exclusive, and the hint says which one is armed.
        view = tk.Frame(bar, bg="#111")
        view.pack(side="left", padx=(18, 4))
        self.live_var = tk.BooleanVar(value=self._live_pref)
        tk.Checkbutton(view, text=" Live view", variable=self.live_var,
                       bg="#111", fg="#ccc", selectcolor="#4a4a4a",
                       activebackground="#111", activeforeground="#fff",
                       highlightthickness=0, font=("Helvetica", 11),
                       command=self._toggle_live).pack(anchor="w")
        self.live_hint = tk.Label(view, bg="#111", fg="#777", font=("Menlo", 9),
                                  text=self._live_hint_text(), anchor="w", width=22)
        self.live_hint.pack(anchor="w", pady=(2, 0))

        # status row: fixed-width message on the left so changing text cannot resize the
        # window, battery indicator pinned right
        # ---- focus stack panel: collapsed by default, and when open the CAPTURE button
        # itself runs the stack, so there is one primary action rather than two buttons
        stack = tk.Frame(root, bg="#1a1a1a")
        stack.grid(row=1, column=0, sticky="we", padx=8, pady=(0, 8))

        self.stack_on = tk.BooleanVar(value=False)
        self.stack_toggle = tk.Checkbutton(
            stack, text="  Focus stack", variable=self.stack_on,
            command=self._toggle_stack, bg="#1a1a1a", fg="#ccc",
            selectcolor="#4a4a4a", activebackground="#1a1a1a",
            activeforeground="#fff", highlightthickness=0,
            font=("Helvetica", 12, "bold"))
        self.stack_toggle.pack(side="left", padx=(8, 10), pady=9)

        self.stack_body = tk.Frame(stack, bg="#1a1a1a")
        self.stack_body.pack(side="left", pady=9)

        self.stack_frames = tk.IntVar(value=3)
        fr_box = tk.Frame(self.stack_body, bg="#1a1a1a")
        fr_box.pack(side="left", padx=(0, 10))
        tk.Label(fr_box, text="frames", bg="#1a1a1a", fg="#888",
                 font=("Helvetica", 10)).pack(anchor="w")
        self.om_frames = tk.OptionMenu(fr_box, self.stack_frames, *STACK_FRAMES)
        self._style_menu(self.om_frames, 5)
        self.om_frames.pack()

        self.stack_step = tk.StringVar(value=STEP_NAMES[0])
        st_box = tk.Frame(self.stack_body, bg="#1a1a1a")
        st_box.pack(side="left", padx=(0, 12))
        tk.Label(st_box, text="step", bg="#1a1a1a", fg="#888",
                 font=("Helvetica", 10)).pack(anchor="w")
        self.om_step = tk.OptionMenu(st_box, self.stack_step, *STEP_NAMES)
        self._style_menu(self.om_step, 11)
        self.om_step.pack()

        self.stack_dir = tk.StringVar(value=DIR_NAMES[0])
        dir_box = tk.Frame(self.stack_body, bg="#1a1a1a")
        dir_box.pack(side="left", padx=(0, 12))
        tk.Label(dir_box, text="direction", bg="#1a1a1a", fg="#888",
                 font=("Helvetica", 10)).pack(anchor="w")
        self.om_dir = tk.OptionMenu(dir_box, self.stack_dir, *DIR_NAMES)
        self._style_menu(self.om_dir, 16)
        self.om_dir.pack()

        row = tk.Frame(root, bg="#111")
        row.grid(row=2, column=0, sticky="we", padx=10, pady=(0, 8))
        row.columnconfigure(0, weight=1)

        self.status = tk.Label(row, text="starting...", anchor="w", bg="#111",
                               fg="#9e9e9e", font=("Menlo", 11), width=58)
        self.status.grid(row=0, column=0, sticky="w")

        # mode as a compact badge rather than "mode=P" in the message text
        self.mode_badge = tk.Label(row, text="--", bg="#2b2b2b", fg="#e8e8e8",
                                   font=("Helvetica", 11, "bold"), width=5,
                                   bd=1, relief="solid")
        self.mode_badge.grid(row=0, column=1, sticky="e", padx=(8, 12))

        batt_box = tk.Frame(row, bg="#111")
        batt_box.grid(row=0, column=2, sticky="e")
        self.batt_canvas = tk.Canvas(batt_box, width=30, height=14, bg="#111",
                                     highlightthickness=0, bd=0)
        self.batt_canvas.pack(side="left", padx=(8, 4))
        self.batt_label = tk.Label(batt_box, text="--%", bg="#111", fg="#9e9e9e",
                                   font=("Menlo", 11), width=4, anchor="e")
        self.batt_label.pack(side="left")

        root.bind("<space>", lambda e: self.on_capture())
        root.bind("<Return>", lambda e: self.on_capture())
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
        # keep the controls above the preview window: ffplay is a separate process and
        # can otherwise sit on top, swallowing every click meant for these controls
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
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
        self.player = subprocess.Popen(
            ["ffplay", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
             "-framedrop", "-autoexit", "-f", "h264",
             "-window_title", "X1D II live view", "-i", "-"],
            stdin=subprocess.PIPE)
        # the preview is its own process and takes focus when it maps; keep the controls
        # in front so a click lands on the button instead of only activating the window
        for delay in (300, 1000, 2500):
            self.root.after(delay, self._raise_controls)
        self._rec = open(record, "wb") if record else None

        for fn in (self._reader, self._writer, self._watchdog, self._reconnect,
                   self._stats, self._poll):
            threading.Thread(target=fn, daemon=True).start()

        self._try_connect()
        self._load_controls()

    # ---- ui helpers

    def _make_control(self, parent, label, code, name_code):
        box = tk.Frame(parent, bg="#111")
        box.pack(side="left", padx=6)
        tk.Label(box, text=label, bg="#111", fg="#888",
                 font=("Helvetica", 10)).pack(anchor="w")
        # tk.OptionMenu rather than ttk.Combobox: the aqua combobox defers its
        # <<ComboboxSelected>> event until focus moves or Return is pressed, so picking
        # a value appeared to do nothing. A menubutton's command fires on the click.
        var = tk.StringVar(value="")
        om = tk.OptionMenu(box, var, "",
                           command=lambda _v, c=code, l=label: self._set_param(c, l))
        om.configure(anchor="w", width=12, highlightthickness=0, bd=1, relief="raised",
                     bg="#2d2d2d", fg="#eeeeee", activebackground="#3d3d3d",
                     activeforeground="#ffffff", font=("Helvetica", 11))
        om["menu"].configure(bg="#2d2d2d", fg="#eeeeee")
        om.pack()
        setattr(self, "var_" + label.lower(), var)
        setattr(self, "om_" + label.lower(), om)
        setattr(self, "vals_" + label.lower(), {})

    # ---- the release button

    def _make_release(self, parent):
        """The release, drawn rather than photographed.

        Modelled on the X release cord: a wide matte grey disc with a thin dark rim, a
        recessed well, and a small matte orange button sitting down inside it - not a
        glossy dome. tk.Button cannot be a circle on aqua (it paints its own rounded
        rectangle and ignores most styling), so this is a Canvas.
        """
        box = tk.Frame(parent, bg="#111")
        box.pack(side="left", padx=(0, 16))
        self.capture_canvas = tk.Canvas(box, width=70, height=70, bg="#111",
                                        highlightthickness=0, bd=0, cursor="hand2")
        self.capture_canvas.pack()
        self.capture_label = tk.Label(box, text="CAPTURE", bg="#111", fg="#9e9e9e",
                                      font=("Helvetica", 9, "bold"), cursor="hand2")
        self.capture_label.pack(pady=(0, 1))
        for w in (box, self.capture_canvas, self.capture_label):
            w.bind("<Button-1>", self._release_click)
            w.bind("<Enter>", lambda e: self._release_hover(True))
            w.bind("<Leave>", lambda e: self._release_hover(False))
        self._capture_enabled = True
        self._release_hovered = False
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
        on = getattr(self, "_capture_enabled", True)
        hot = getattr(self, "_release_hovered", False) and on
        core = BTN_ORANGE if on else "#6b6b6b"
        if hot and on:
            core = _mix(core, "#ffffff", 0.10)
        cx = cy = 35.0
        r_out, r_well, r_core = 33.0, 21.5, 17.0
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

        self.capture_label.configure(
            fg=(BTN_ORANGE if (hot and on) else "#9e9e9e" if not on else "#d6d6d6"))


    def _toggle_stack(self):
        """Enable the stack controls. Nothing is packed or unpacked and the capture
        button never changes its label, so the window cannot resize as a side effect."""
        on = self.stack_on.get()
        state = "normal" if on else "disabled"
        for widget in (self.om_frames, self.om_step, self.om_dir):
            try:
                widget.configure(state=state)
            except Exception:
                pass
        self._log("focus stack mode -> %s" % on)

    def _flat_button(self, parent, text, command, bg="#2d2d2d"):
        """A dark button. tk.Button on aqua draws its own light face and ignores bg, so a
        styled label is the only way to keep the panel's colours consistent."""
        b = tk.Label(parent, text=text, bg=bg, fg="#ededed",
                     font=("Helvetica", 10), padx=9, pady=3, cursor="hand2")
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.configure(bg="#3d3d3d"))
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        return b

    def _raise_controls(self):
        """Activate the window the way clicking its title bar does.

        On macOS a window can be visible and topmost yet not be the key window, in which
        case clicks on its buttons never reach the process at all. Re-asserting topmost
        and forcing focus is what a title-bar click would do; it is retried a few times
        because ffplay's window appears shortly after startup and can take focus back.
        """
        try:
            self.root.lift()
            self.root.attributes("-topmost", False)
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            self.root.focus_set()
        except Exception:
            pass

    def _style_menu(self, om, width):
        om.configure(anchor="w", width=width, highlightthickness=0, bd=1,
                     relief="raised", bg="#2d2d2d", fg="#eeeeee",
                     activebackground="#3d3d3d", activeforeground="#ffffff",
                     font=("Helvetica", 11))
        om["menu"].configure(bg="#2d2d2d", fg="#eeeeee")

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
        from tkinter import filedialog
        d = filedialog.askdirectory(title="Where should captures be saved?",
                                    initialdir=self.download_dir or os.path.expanduser("~"))
        self.root.lift()
        self.root.focus_force()          # a modal dialogue leaves focus behind
        if d:
            self.download_dir = d
            self.dir_label.configure(text=os.path.basename(d) or d)
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
                self.dir_label.configure(text="(no folder chosen)")
                self._say("no folder chosen - pick one with folder…")
            threading.Thread(target=self._ensure_session, daemon=True).start()

    # ---- live view / cable release

    def _live_hint_text(self):
        return "cable release armed" if not self._live_pref else "body shutter blocked"

    def _save_pref(self):
        """Persist the three choices that survive a restart."""
        try:
            import json
            cfg = {}
            try:
                cfg = json.load(open(self._cfg))
            except Exception:
                pass
            cfg["live_view"] = bool(self.live_var.get())
            cfg["save_copy"] = bool(self.save_var.get())
            if self.download_dir:
                cfg["download_dir"] = self.download_dir
            json.dump(cfg, open(self._cfg, "w"))
        except Exception:
            pass

    def _toggle_live(self):
        """Swap between previewing and arming the cable release.

        While the preview streams the body ignores its own shutter button and the cable
        release; stopping the stream brings it straight back, so this is a switch rather
        than a reconnect. Captures still need a tether session to be offered to the host,
        so turning the preview off makes sure one exists.
        """
        on = self.live_var.get()
        self._live_pref = bool(on)
        self._save_pref()
        self.live_hint.configure(text=self._live_hint_text())
        self._log("live view -> %s" % on)
        if on:
            try:
                self._start_live()
            except Exception as e:
                self._log("live view on failed: %s" % e)
            self._say("live view on - the body shutter cannot fire while streaming")
        else:
            try:
                self._stop_live()
            except Exception as e:
                self._log("live view off failed: %s" % e)
            if not self.session_ok:
                threading.Thread(target=self._ensure_session, daemon=True).start()
            else:
                self._say("live view off - press the cable release on the camera")

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
        if self.session_ok or self.cam is None:
            return
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
                         getattr(self.cam, "last_session_sent", 0)))
            if not self.session_ok:
                self._say("session incomplete (%d commands) - capture may not transfer"
                          % len(failed))
            elif self._live_pref:
                self._say("session ready - captures will be saved and kept on the card")
            else:
                self._say("session ready - press the cable release on the camera")
        except Exception as e:
            self._log("session failed: %s" % e)
            self._say("session failed: %s" % e)
        finally:
            self.downloading = False

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
            # the tether session is what we need instead. Start it only once the event
            # loop is running - Tk cannot be touched from a worker before mainloop().
            self._log("connect: live view off, establishing cable-release session")
            self.root.after(600, lambda: threading.Thread(
                target=self._ensure_session, daemon=True).start())
        self.disconnected = False
        self._recover_until = time.time() + 3
        return True

    def _reconnect(self):
        while self.running:
            time.sleep(1.5)
            if self.running and self.disconnected:
                self._log("reconnect: retrying")
                if self._try_connect():
                    self._say("camera reconnected")
                    self._load_controls()

    def _start_live(self):
        with self.lock:
            self.cam.call(hbctl.OP_CMD, 0x0007, bytes(10), retries=0)
            time.sleep(0.4)
            r = self.cam.call(hbctl.OP_CMD, 0x0007, bytes([0x0C]) + bytes(9))
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

    def _load_controls(self):
        if self.cam is None:
            return
        with self.lock:
            for code, label, name_code in ((0x0008, "Aperture", 0x003e),
                                           (0x0009, "Shutter", 0x003f),
                                           (0x000a, "ISO", None)):
                try:
                    desc = self.cam.describe(code)
                    cur = self.cam.read(code)
                except Exception:
                    continue
                values, order = {}, []
                if label == "ISO":
                    m = re.match(r"^LF\d+,(.*)$", desc or "")
                    for h in (m.group(1).split(",") if m else []):
                        try:
                            n = round(struct.unpack(
                                ">d", bytes.fromhex(h.rjust(16, "0")))[0])
                        except Exception:
                            continue
                        text = "Auto" if n == -1 else str(n)
                        values[text] = n
                        order.append(text)
                else:
                    vals = descriptor_list(desc)
                    names = name_table(self.cam.describe(name_code)) if name_code else []
                    if names and len(names) == len(vals):
                        order = list(names)
                        values = dict(zip(order, vals))
                    elif names:
                        for n in names:
                            if n in ("T", "B"):
                                continue
                            v = rational(n)
                            if v is not None:
                                order.append(n)
                                values[n] = "r%x" % v
                    else:
                        order = list(vals)
                        values = {v: v for v in vals}
                om = getattr(self, "om_" + label.lower())
                var = getattr(self, "var_" + label.lower())
                menu = om["menu"]
                menu.delete(0, "end")
                for choice in order:
                    menu.add_command(label=choice,
                                     command=lambda v=choice, l=label: self._choose(l, v))
                setattr(self, "vals_" + label.lower(), values)
                if order:
                    match = [k for k, v in values.items() if _same(v, cur)]
                    var.set(match[0] if match else order[0])
                    om.configure(state="normal")
                else:
                    var.set("n/a")
                    om.configure(state="disabled")
                self._applied[label] = var.get()
                self._log("%s: %d choices current=%r" % (label, len(order), cur))

    def _draw_mode(self, name, accent):
        self.mode_badge.configure(text=name, bg=accent)

    def _draw_battery(self, pct):
        """Small battery glyph plus the number, so the row never reflows."""
        c = self.batt_canvas
        c.delete("all")
        x0, y0, x1, y1 = 1, 2, 24, 12
        c.create_rectangle(x0, y0, x1, y1, outline="#8a8a8a", width=1)
        c.create_rectangle(x1 + 1, y0 + 3, x1 + 4, y1 - 3, fill="#8a8a8a", outline="")
        if pct is None:
            self.batt_label.configure(text="--%")
            return
        pct = max(0, min(100, int(pct)))
        span = (x1 - x0 - 2)
        fill = max(1, int(span * pct / 100.0))
        colour = "#4caf50" if pct > 50 else "#ffb300" if pct > 20 else "#e53935"
        c.create_rectangle(x0 + 1, y0 + 1, x0 + 1 + fill, y1 - 1, fill=colour, outline="")
        self.batt_label.configure(text="%d%%" % pct)

    def _apply_mode_state(self, mode, aperture, shutter):
        """Grey out whatever the current exposure mode does not allow.

        The mode cannot be set over USB (the write is refused with ack 0x0d) - the body
        owns it - so this only mirrors what the camera reports.
        """
        allow_ap, allow_sh = MODE_ALLOWS.get(mode, (True, True))
        # belt and braces: an inapplicable control also reads the 7fffffff sentinel
        allow_ap = allow_ap and aperture is not None and str(aperture) != "7fffffff"
        allow_sh = allow_sh and shutter is not None and str(shutter) != "7fffffff"
        self._set_enabled("Aperture", allow_ap)
        self._set_enabled("Shutter", allow_sh)

    def _set_enabled(self, label, enabled):
        om = getattr(self, "om_" + label.lower())
        cur = str(om.cget("state"))
        if enabled and cur in ("normal", "active"):
            return                      # 'active' means the pointer is over it
        if not enabled and cur == "disabled":
            return
        om.configure(state="normal" if enabled else "disabled")
        self._log("%s %s" % (label, "enabled" if enabled else "greyed out"))

    def _choose(self, label, value):
        """OptionMenu callback: set the displayed value, then apply it."""
        self._log("choose %s = %r" % (label, value))
        getattr(self, "var_" + label.lower()).set(value)
        self._set_param(self._combo_code[label], label)

    def _set_param(self, code, label):
        values = getattr(self, "vals_" + label.lower())
        text = getattr(self, "var_" + label.lower()).get()
        if text not in values:
            return
        raw = values[text]
        self._log("apply %s = %r (%r)" % (label, text, raw))
        self._applied[label] = text
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
        if not getattr(self, "_capture_enabled", True):
            return                   # a shot or a stack is already running
        if self.stack_on.get():
            self.on_stack()          # the panel is open, so CAPTURE means "run the stack"
            return
        self._log("capture: click received")
        self._set_capture_enabled(False)
        self.root.update_idletasks()

        def run():
            self.capturing = True
            self._saved_path = None
            if self.save_var.get() and not self.download_dir:
                self._say("no folder chosen - this shot goes to the card only")
            want_save = bool(self.save_var.get() and self.download_dir)
            if want_save and not self.session_ok:
                self._say("waiting for the tether session to finish ...")
                deadline = time.time() + 90
                while time.time() < deadline and not self.session_ok and self.running:
                    time.sleep(0.3)
            saving = bool(want_save and self.session_ok)
            token = None
            try:
                if saving:
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
            if token and saving:
                try:
                    self._say("downloading capture ...")
                    dest = os.path.join(self.download_dir, "capture_%04x.3fr" % token)
                    self.downloading = True      # stop the preview stealing 0x84
                    time.sleep(0.4)              # let the reader notice and stand down
                    try:
                        with self.lock:
                            n = self.cam.download(token, dest)
                    finally:
                        self.downloading = False
                    self._log("capture: saved %s (%d bytes, expected %r)"
                              % (dest, n, getattr(self.cam, "last_download_expected", None)))
                    self._saved_path = dest if n else None
                except Exception as e:
                    self._log("capture: download failed %s" % e)
            self._recover_until = time.time() + 8
            time.sleep(1.2)
            if self._live_pref:
                try:
                    self._start_live()
                except Exception as e:
                    self._log("capture: restart failed %s" % e)
            self.root.after(0, lambda: self._set_capture_enabled(True))
            if token is None:
                self._say("capture failed - is live view running?")
            elif self._saved_path:
                self._say("saved %s" % os.path.basename(self._saved_path))
            else:
                self._say("captured (token 0x%04x)" % token)
        threading.Thread(target=run, daemon=True).start()

    def on_stack(self):
        """Run the camera's focus bracketing. Live view must be off for the sequence."""
        if self.capturing or self.cam is None or self.disconnected:
            return
        if self.session_ok:
            # in a session the camera hands frames to the host instead of the card, so
            # bracketing produces nothing and the camera reports a failed transfer
            self._say("turn off Save a copy and reconnect - tethered stacks go to the host")
            self._log("stack refused: tether session active")
            return
        frames = int(self.stack_frames.get())
        step = STEP_NAMES.index(self.stack_step.get())
        self.capturing = True
        self._log("stack: start %d frames, step %s, %s"
                  % (frames, self.stack_step.get(), self.stack_dir.get()))
        self._set_capture_enabled(False)

        def run():
            got = []
            try:
                self._say("stack: stopping preview\u2026")
                # Stop live view and CONFIRM the stream has stopped. With the preview
                # running the camera accepts the trigger but ignores the drive mode, so a
                # bracket silently becomes one frame - which is exactly what happened.
                for attempt in range(4):
                    with self.lock:
                        self.cam.call(hbctl.OP_CMD, 0x0007, bytes(10), retries=0)
                    quiet = time.time()
                    stopped = False
                    while time.time() - quiet < 1.5:
                        if self._last_frame_time > quiet:
                            break                 # a frame arrived: still streaming
                        time.sleep(0.1)
                    else:
                        stopped = True
                    self._log("stack: stop preview attempt %d, stopped=%s" % (attempt + 1, stopped))
                    if stopped:
                        break
                self.downloading = True          # the reader must not take 0x84
                time.sleep(0.6)
                self._say("stack: starting\u2026")
                direction = DIR_NAMES.index(self.stack_dir.get())
                with self.lock:
                    got = self.cam.focus_bracket(
                        frames=frames, step=step, direction=direction,
                        progress=lambda p, n: self._say("stack %d/%d" % (n, frames)))
                self._log("stack: %d/%d frames" % (len(got), frames))
                if not got:
                    self._say("stack: no frames - camera busy, try again")
                if len(got) >= frames:
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
                self.root.after(0, lambda: self._set_capture_enabled(True))
        threading.Thread(target=run, daemon=True).start()
        # a wedged camera can block a USB read indefinitely; do not let that hold the UI
        self.root.after(int((frames * 10 + 45) * 1000), self._stack_watchdog)

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
                plen = int.from_bytes(frame[12:16], "little")
                payload = frame[24:24 + plen]
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
        cached = getattr(self, "_cache_path", None)
        if cached:
            return cached
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
        """Pull a body-triggered capture to the host and show it.

        With Save a copy off the transfer is only needed to make the still, so the raw
        file is deleted once the JPEG is out of it - otherwise every shot would leave
        another 100 MB behind for a picture the card already holds.
        """
        saving = bool(self.save_var.get() and self.download_dir)
        if saving:
            dest = os.path.join(self.download_dir, "capture_%04x.3fr" % token)
        else:
            dest = os.path.join(self._cache_dir(), "capture_%04x.3fr" % token)
        self._say("camera shot 0x%04x - downloading ..." % token)
        try:
            self.downloading = True          # the reader must release 0x84
            time.sleep(0.4)
            with self.lock:
                n = self.cam.download(token, dest)
            self._log("external capture %s (%d bytes, expected %r)"
                      % (dest, n, getattr(self.cam, "last_download_expected", None)))
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
        except Exception as e:
            self._log("external capture failed %s" % e)
            self._say("camera shot download failed - %s" % type(e).__name__)
        finally:
            self.downloading = False
            self._recover_until = time.time() + 8

    def _show_still(self, raw_path):
        """Put the captured frame in the preview window.

        ffplay is already the preview, so the still is handed to it the same way - the
        embedded JPEG is pulled out of the 3FR, converted to PNG for Tk, and shown in a
        window of its own. The live H.264 player is left alone so toggling live view back
        on needs no restart.
        """
        try:
            data = open(raw_path, "rb").read(48 << 20)
        except Exception as e:
            self._log("still: cannot read %s (%s)" % (raw_path, e))
            return
        i = data.find(b"\xff\xd8\xff")
        j = data.rfind(b"\xff\xd9")
        if i < 0 or j <= i:
            self._log("still: no embedded JPEG in %s" % raw_path)
            return
        cache = self._cache_dir()
        jpg = os.path.join(cache, "still.jpg")
        png = os.path.join(cache, "still.png")
        try:
            with open(jpg, "wb") as fh:
                fh.write(data[i:j + 2])
            subprocess.run(["sips", "-s", "format", "png", "-Z", "1800", jpg,
                            "--out", png],
                           check=False, capture_output=True, timeout=20)
            os.remove(jpg)               # 45 MB, needed only for the conversion
        except Exception as e:
            self._log("still: conversion failed %s" % e)
            return
        self._log("still: %d bytes of JPEG -> %s" % (j - i + 2, png))
        self.root.after(0, lambda: self._still_window(png))

    def _still_window(self, png):
        try:
            img = tk.PhotoImage(file=png)
        except Exception as e:
            self._log("still: Tk cannot load %s (%s)" % (png, e))
            return
        win = getattr(self, "_still_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.configure()
                win.children["img"].configure(image=img)
                win.image = img
                win.lift()
                return
        except Exception:
            pass
        win = tk.Toplevel(self.root)
        win.title("X1D II capture")
        win.configure(bg="#000")
        lab = tk.Label(win, image=img, bg="#000", name="img")
        lab.pack()
        win.image = img                     # keep a reference or Tk drops the pixels
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        self._still_win = win

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
                    self.cam.call(4, 0x0003, b"", retries=0)
                    self.cam.call(4, 0x0002, b"", retries=0)
                    for v in (b"\x07\x00\x00\x00", b"\x08\x00\x00\x00"):
                        self.cam.call(4, 0x0001, v, retries=0)
            except Exception:
                pass

    def _writer(self):
        while self.running:
            with self.payloads_lock:
                payload = self.payloads.popleft() if self.payloads else None
            if payload is None:
                time.sleep(0.002)
                continue
            if self._rec:
                self._rec.write(payload)
            self._frames_seen += 1
            self.times.append(time.time())
            self._last_frame_time = time.time()
            try:
                self.player.stdin.write(payload)
                self.player.stdin.flush()
            except Exception as e:
                self._log("writer: %s" % type(e).__name__)
                return

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
            if self._mode in (8, 10):
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
            batt = ap = sh = mode = None
            try:
                if self.cam is not None:
                    with self.lock:
                        batt = self.cam.read(0x002f)
                        ap = self.cam.read(0x0008)
                        sh = self.cam.read(0x0009)
                        mode = self.cam.read(0x0003)
                        self._mode = mode
                    self.root.after(0, lambda m=mode, a=ap, s=sh:
                                    self._apply_mode_state(m, a, s))
            except Exception as e:
                self._mark_lost("stats: %s" % type(e).__name__)

            cutoff = time.time() - 2.0
            fps = sum(1 for t in self.times if t >= cutoff) / 2.0
            if self.disconnected:
                state = "camera disconnected - waiting"
            elif not self._live_pref:
                # no preview is deliberate here: the body shutter only works in a
                # session that never started live view
                if self.session_ok:
                    state = "cable release armed - press the body shutter"
                else:
                    state = "arming cable release (session) ..."
            elif self._live_refused:
                held = time.time() - self._refused_since if self._refused_since else 0.0
                if mode == 8:
                    state = "Mq - no live view by design"
                elif mode == 10:
                    # video mode does not tether a preview at all - Phocus cannot either
                    state = "video mode - no tether preview"
                elif held > 20:
                    state = "refused for %ds - likely too hot, let it cool" % int(held)
                else:
                    state = "refused live view - wake the screen / exit menus"
            elif time.time() < self._recover_until:
                state = "recovering (camera writing)"
            elif time.time() - self._last_frame_time > 6:
                state = ("no preview - Mq has no live view by design"
                         if mode == 8 else "no preview - check the camera body")
            else:
                state = "live"

            if (self.cam is not None
                    and time.time() - getattr(self, "_last_pkt_log", 0) > 20):
                self._last_pkt_log = time.time()
                self._log("packets: %s" % {hex(k): v for k, v in
                                           getattr(self.cam, "pkt_stats", {}).items()})
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
        self.running = False
        try:
            if self.cam is not None:
                with self.lock:
                    self.cam.call(hbctl.OP_CMD, 0x0007, bytes(10), retries=0)
        except Exception:
            pass
        if self._rec:
            self._rec.close()
        try:
            self.player.kill()
        except Exception:
            pass
        try:
            if self.cam is not None:
                self.cam.close()
        except Exception:
            pass
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
