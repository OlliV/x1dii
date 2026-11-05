#!/usr/bin/env python3
"""Tethering control window for the Hasselblad X1D II.

Video goes to ffplay in its own window; this window is only the controls. That split is
deliberate: Tk's photo-image path costs real compositing time per frame and made the
buttons feel dead, while ffplay is a native video renderer and stays smooth.

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
import subprocess
import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import hbctl              # noqa: E402
from lib import liveview as ls     # noqa: E402

import usb.core                    # noqa: E402  must come after hbctl

QUEUE_MAX = 8
BTN_BG = "#f0f0f0"

# Exposure mode as reported by 0x0003. The body owns this and the host cannot set it
# (a write is refused with ack 0x0d), so these are only ever read.
# Mapped from the dial by observation - see analysis/README.md:
#   9 = full auto (square), 3 = P, 2 = S, 1 = A, 0 = M, 8 = Mq, 10 = video
# A C slot reports whatever mode it is programmed to (so a C set to A reads 1), which
# means the greying rules follow it automatically.
MODE_NAMES = {9: "\u25a1", 3: "P", 2: "S", 1: "A", 0: "M", 8: "Mq", 10: "Video"}

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
        self._logf = open("/tmp/viewer.log", "a", buffering=1)
        self._log("--- viewer start ---")

        try:
            import json
            saved = json.load(open(self._cfg)).get("download_dir")
            if saved and os.path.isdir(saved):
                self.download_dir = saved
        except Exception:
            pass

        # ---- controls
        root.title("X1D II tether")
        root.configure(bg="#111")

        bar = tk.Frame(root, bg="#111")
        bar.grid(row=0, column=0, sticky="we", padx=8, pady=8)

        self.capture_btn = tk.Button(bar, text="●  CAPTURE", command=self.on_capture,
                                     font=("Helvetica", 14, "bold"), padx=18, pady=8,
                                     default="active", bg=BTN_BG)
        self.capture_btn.pack(side="left", padx=(0, 18))

        self._make_control(bar, "Aperture", 0x0008, 0x003e)
        self._make_control(bar, "Shutter", 0x0009, 0x003f)
        self._make_control(bar, "ISO", 0x000a, None)

        save = tk.Frame(bar, bg="#111")
        save.pack(side="left", padx=(18, 4))
        self.save_var = tk.BooleanVar(value=False)
        tk.Checkbutton(save, text=" Save a copy", variable=self.save_var,
                       bg="#111", fg="#ccc", selectcolor="#4a4a4a",
                       activebackground="#111", activeforeground="#fff",
                       highlightthickness=0, font=("Helvetica", 11),
                       command=self._toggle_save).pack(anchor="w")
        tk.Button(save, text="folder…", command=self._pick_dir,
                  font=("Helvetica", 10), padx=4).pack(anchor="w", pady=(2, 0))
        self.dir_label = tk.Label(save, bg="#111", fg="#777", font=("Menlo", 9),
                                  text=(os.path.basename(self.download_dir)
                                        if self.download_dir else "(card only)"))
        self.dir_label.pack(anchor="w")

        self.status = tk.Label(root, text="starting...", anchor="w", bg="#111",
                               fg="#9e9e9e", font=("Menlo", 11))
        self.status.grid(row=1, column=0, sticky="we", padx=10, pady=(0, 8))

        root.bind("<space>", lambda e: self.on_capture())
        root.bind("<Return>", lambda e: self.on_capture())
        root.bind("<Enter>", lambda e: root.focus_force())
        root.protocol("WM_DELETE_WINDOW", self.on_close)

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
        self._rec = open(record, "wb") if record else None

        for fn in (self._reader, self._writer, self._watchdog, self._reconnect,
                   self._stats):
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

    # ---- saving

    def _pick_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="Where should captures be saved?")
        self.root.lift()
        self.root.focus_force()          # a modal dialogue leaves focus behind
        if d:
            self.download_dir = d
            self.dir_label.configure(text=os.path.basename(d) or d)
            self._log("save dir = %s" % d)
            try:
                import json
                json.dump({"download_dir": d}, open(self._cfg, "w"))
            except Exception:
                pass
            if self.save_var.get():
                threading.Thread(target=self._ensure_session, daemon=True).start()

    def _toggle_save(self):
        on = self.save_var.get()
        self._log("save a copy -> %s" % on)
        if on:
            if not self.download_dir:
                self.dir_label.configure(text="(no folder chosen)")
                self._say("no folder chosen - pick one with folder…")
            threading.Thread(target=self._ensure_session, daemon=True).start()

    def _ensure_session(self):
        if self.session_ok or self.cam is None:
            return
        self._say("establishing tether session (about 40 s, one time) ...")
        try:
            with self.lock:
                self.cam.replay_session()
                self.cam.flush_stream(2.0)
            self.session_ok = True
            self._log("session established")
            self._say("session ready - captures will be saved and kept on the card")
        except Exception as e:
            self._log("session failed: %s" % e)
            self._say("session failed: %s" % e)

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
        try:
            self._start_live()
        except Exception as e:
            self._log("connect: live view failed (%s)" % type(e).__name__)
            self.disconnected = True
            return False
        self.disconnected = False
        self._recover_until = time.time() + 3
        self._log("connect: live view started")
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
        self._log("capture: click received")
        self.capture_btn.configure(text="●  …", state="disabled")
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
                    with self.lock:
                        n = self.cam.download(token, dest)
                    self._log("capture: saved %s (%d bytes)" % (dest, n))
                    self._saved_path = dest if n else None
                except Exception as e:
                    self._log("capture: download failed %s" % e)
            self._recover_until = time.time() + 8
            time.sleep(1.2)
            try:
                self._start_live()
            except Exception as e:
                self._log("capture: restart failed %s" % e)
            self.root.after(0, lambda: self.capture_btn.configure(
                text="●  CAPTURE", state="normal"))
            if token is None:
                self._say("capture failed - is live view running?")
            elif self._saved_path:
                self._say("saved %s" % os.path.basename(self._saved_path))
            else:
                self._say("captured (token 0x%04x)" % token)
        threading.Thread(target=run, daemon=True).start()

    # ---- pipeline

    def _reader(self):
        buf = b""
        epoch = -1
        while self.running:
            if epoch != self._epoch:
                epoch = self._epoch
                buf = b""
                self._synced = False
            if self.cam is None:
                time.sleep(0.2)
                continue
            try:
                buf += bytes(self.cam.dev.read(hbctl.EP_DATA, 1 << 18, timeout=80))
            except usb.core.USBTimeoutError:
                continue
            except Exception as e:
                self._mark_lost("reader: %s" % type(e).__name__)
                time.sleep(0.5)
                continue
            frames, buf = ls.frames_from(buf)      # drops control packets
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

            text = "%.1f fps   %s   mode=%s   battery=%s%%" % (
                fps, state, MODE_NAMES.get(mode, "?"),
                batt if batt is not None else "?")
            self.root.after(0, lambda t=text: self.status.configure(text=t))
            self._log("fps=%.1f frames=%d drops=%d state=%s mode=%r"
                      % (fps, self._frames_seen, self.drops, state, mode))

    def _say(self, text):
        self.root.after(0, lambda: self.status.configure(text=text))

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
