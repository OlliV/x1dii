#!/usr/bin/env python3
"""Tethering viewer for the Hasselblad X1D II.

Live preview, shutter release and exposure controls, talking to the camera directly
over USB with no Phocus and no SDK.

    python3 viewer.py [--scale WxH] [--record out.h264]

Pipeline (every stage may drop frames, so nothing ever blocks the camera):

  reader thread    0x84 -> H.264 payloads -> deque(maxlen=N), drops the oldest
  writer thread    deque -> ffmpeg stdin (blocking, isolated from the reader)
  decoder thread   ffmpeg stdout -> raw RGB -> latest-frame slot
  main thread      Tk: pull latest frame, blit to a Label
  worker thread    camera commands (capture, exposure) - never blocks the UI

Frames are dropped only if the deque overflows, because dropping breaks the H.264
reference chain. The reader must never be blocked by the decoder for the same reason the
first version froze: back-pressure fills the camera's buffers and nothing recovers.
"""
import collections
import re
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import hbctl          # noqa: E402  (adds /tmp/pylibs to sys.path)
import liveview_stream as ls

import usb.core       # noqa: E402  must come after hbctl

FPS_WINDOW = 24
QUEUE_MAX = 8


def descriptor_list(desc):
    """'LR11,a,b,...' -> ['a','b',...]"""
    if not desc:
        return []
    m = re.match(r"^L[A-Za-z](\d+),(.*)$", desc)
    return m.group(2).split(",") if m else []


def rational(name):
    """Shutter/aperture values are rationals: (numerator << 16) | denominator.

    '1/125' -> 0x1007d, '1.4' -> 0xe000a, '45' -> 0x2d0001, '1:08' -> 0x6c0064
    """
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


def name_table(desc):
    """'LS34,4\\t4.8\\t5.6' -> ['4','4.8','5.6']  (first number is string length)"""
    if not desc:
        return []
    m = re.match(r"^LS\d+,(.*)$", desc)
    return m.group(1).split("\t") if m else []


def _same(a, b):
    """Compare a dropdown value with what the camera reports, numerically if possible."""
    if isinstance(a, str) and isinstance(b, str) and a == b:
        return True
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return str(a) == str(b)


class App:
    def __init__(self, root, scale, record=None):
        self.root = root
        self.W, self.H = scale
        self.frame_bytes = self.W * self.H * 3
        self.running = True
        self.lock = threading.Lock()          # serialises camera commands
        self.payloads = collections.deque(maxlen=QUEUE_MAX)
        self.payloads_lock = threading.Lock()
        self.frames = collections.deque(maxlen=1)
        self.frames_lock = threading.Lock()
        self.times = collections.deque(maxlen=FPS_WINDOW)
        self.drops = 0
        self._synced = False
        self._epoch = 0
        self.capturing = False
        self._recover_until = 0.0
        self._frames_seen = 0
        self._img = None
        self._shown = -1
        self._last_frame_time = time.time()
        self._rec_file = open(record, "wb") if record else None
        self._logf = open("/tmp/viewer.log", "a", buffering=1)
        self._log("--- viewer start %dx%d ---" % (self.W, self.H))

        self.cam = None
        self.disconnected = True

        root.title("X1D II tether")
        root.configure(bg="#111")
        # no width/height: on a text-less Label those are in CHARACTERS, not pixels,
        # which makes the window thousands of pixels wide before the first frame
        self.view = tk.Label(root, bg="#000", fg="#666", text="starting...")
        self.view.grid(row=0, column=0, columnspan=6, padx=6, pady=6)

        bar = tk.Frame(root, bg="#111")
        bar.grid(row=1, column=0, columnspan=6, sticky="we", padx=6, pady=4)

        # native button: macOS swallows the first click on an inactive window with a
        # bare Label, so presses appear to be lost until the window is focused
        self.capture_btn = tk.Button(bar, text="●  CAPTURE", command=self.on_capture,
                                     font=("Helvetica", 13, "bold"),
                                     padx=16, pady=6, default="active")
        self.capture_btn.pack(side="left", padx=(0, 16))

        self._make_control(bar, "Aperture", 0x0008, 0x003e)
        self._make_control(bar, "Shutter", 0x0009, 0x003f)
        self._make_control(bar, "ISO", 0x000a, None)

        self.status = tk.Label(root, text="", anchor="w", bg="#111", fg="#9e9e9e",
                               font=("Menlo", 11))
        self.status.grid(row=2, column=0, columnspan=6, sticky="we", padx=8, pady=(0, 6))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        # diagnostics: log every click so we can tell "no event" from "wrong widget"
        root.bind("<Button-1>", lambda e: self._log(
            "click widget=%s at %d,%d" % (e.widget.__class__.__name__, e.x_root, e.y_root)))
        root.bind("<space>", lambda e: self.on_capture())
        root.bind("<Return>", lambda e: self.on_capture())

        self.ff = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
             "-f", "h264", "-i", "pipe:0",
             "-vf", "scale=%d:%d" % (self.W, self.H),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        # stdin stays BLOCKING on purpose. A pipe holds ~64 KB and a frame payload is
        # ~80 KB, so an O_NONBLOCK write truncates mid-frame and corrupts the stream.
        # The writer thread isolates any wait; the deque absorbs bursts.

        for fn in (self._reader, self._writer, self._decoder, self._watchdog,
                   self._reconnect):
            threading.Thread(target=fn, daemon=True).start()

        self._try_connect()
        self._tick()
        self._stats()

    # ---- ui helpers

    def _make_control(self, parent, label, code, name_code):
        box = tk.Frame(parent, bg="#111")
        box.pack(side="left", padx=6)
        tk.Label(box, text=label, bg="#111", fg="#888",
                 font=("Helvetica", 10)).pack(anchor="w")
        cb = ttk.Combobox(box, width=12, state="readonly")
        cb.pack()
        cb.bind("<<ComboboxSelected>>", lambda e, c=code, l=label: self._set_param(c, l))
        cb.bind("<Return>", lambda e, c=code, l=label: self._set_param(c, l))
        setattr(self, "cb_" + label.lower(), cb)
        setattr(self, "vals_" + label.lower(), {})
        return cb

    # ---- connection supervision

    def _try_connect(self):
        """Open the camera and bring the stream up. Safe to call repeatedly."""
        if self.disconnected:
            # a stale libusb context survives a device re-enumeration
            hbctl.reset_usb()
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
        self._epoch += 1                 # reader flushes its buffer
        self._synced = False
        self._last_frame_time = time.time()
        try:
            self._start_live()
            self._load_controls()
        except Exception as e:
            self._log("connect: bring-up failed (%s)" % type(e).__name__)
            self.disconnected = True
            return False
        self.disconnected = False
        self._recover_until = time.time() + 3
        self._log("connect: live view started")
        return True

    def _reconnect(self):
        """The camera can drop off the bus (reboot, cable, crash). Keep trying."""
        while self.running:
            time.sleep(1.5)
            if not self.running:
                return
            if not self.disconnected:
                continue
            self._log("reconnect: retrying")
            if self._try_connect():
                self._say("camera reconnected")

    def _mark_lost(self, why):
        if not self.disconnected:
            self._log("connection lost: %s" % why)
            self.disconnected = True

    # ---- camera

    def _start_live(self):
        # stop first, then start: joining a stream that is already running leaves us
        # mid-GOP with no SPS, which the decoder cannot sync to
        with self.lock:
            self.cam.call(hbctl.OP_CMD, 0x0007, bytes(10), retries=0)
            time.sleep(0.4)
            r = self.cam.call(hbctl.OP_CMD, 0x0007, bytes([0x0C]) + bytes(9))
        # bump the epoch so the reader throws away its buffer: stale bytes otherwise
        # mis-parse and swallow the new stream's SPS frame
        self._epoch += 1
        return r

    def _stop_live(self):
        with self.lock:
            self.cam.call(hbctl.OP_CMD, 0x0007, bytes(10), retries=0)

    def _load_controls(self):
        with self.lock:
            for code, label, name_code in ((0x0008, "Aperture", 0x003e),
                                           (0x0009, "Shutter", 0x003f),
                                           (0x000a, "ISO", None)):
                desc = self.cam.describe(code)
                cur = self.cam.read(code)
                cb = getattr(self, "cb_" + label.lower())
                values, order = {}, []

                if label == "ISO":
                    m = re.match(r"^LF\d+,(.*)$", desc or "")
                    for h in (m.group(1).split(",") if m else []):
                        try:
                            n = round(struct.unpack(
                                ">d", bytes.fromhex(h.rjust(16, "0")))[0])
                        except Exception:
                            continue
                        text = "Auto" if n == -1 else str(n)   # -1 is the auto sentinel
                        values[text] = n
                        order.append(text)
                else:
                    vals = descriptor_list(desc)
                    names = name_table(self.cam.describe(name_code)) if name_code else []
                    if names and len(names) == len(vals):
                        order = list(names)
                        values = dict(zip(order, vals))
                    elif names:
                        # no value list (photo mode hides it): derive the rationals.
                        # T and B are special exposure modes, not numeric values.
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

                cb["values"] = order
                setattr(self, "vals_" + label.lower(), values)
                if order:
                    match = [k for k, v in values.items() if _same(v, cur)]
                    cb.set(match[0] if match else order[0])
                    cb.configure(state="readonly")
                else:
                    cb.set("n/a")
                    cb.configure(state="disabled")
                self._log("%s: %d choices current=%r" % (label, len(order), cur))

    def _set_param(self, code, label):
        cb = getattr(self, "cb_" + label.lower())
        values = getattr(self, "vals_" + label.lower())
        text = cb.get()
        if text not in values:
            return
        raw = values[text]
        self._say("%s: setting %s ..." % (label, text))

        def run():
            with self.lock:
                ok, before, after, ack = self.cam.write(code, raw)
            self._say("%s %s: %r -> %r%s" % (label, text, before, after,
                                              "" if ok else "   (NOT APPLIED)"))
        threading.Thread(target=run, daemon=True).start()

    def on_capture(self):
        self._log("capture: click received")
        self.capture_btn.configure(text="●  …", state="disabled")
        self.root.update_idletasks()

        def run():
            self.capturing = True
            try:
                with self.lock:
                    self._log("capture: releasing")
                    token, acks = self.cam.release(timeout=12)
                self._log("capture: token=%r acks=%s" % (token, acks))
            except Exception as e:
                self._log("capture: EXCEPTION %s: %s" % (type(e).__name__, e))
                token = None
            finally:
                self.capturing = False
            # the body stops the preview and writes a large RAW before it can resume.
            # Give it a moment, then nudge once; the watchdog covers the rest.
            self._recover_until = time.time() + 10
            time.sleep(1.5)
            try:
                self._start_live()
                self._log("capture: preview restart sent")
            except Exception as e:
                self._log("capture: restart failed %s" % e)
            self.root.after(0, lambda: self.capture_btn.configure(
                text="●  CAPTURE", state="normal"))
            self._say("captured (token 0x%04x)" % token if token else "capture failed - is live view running?")
        threading.Thread(target=run, daemon=True).start()

    # ---- pipeline

    def _watchdog(self):
        """If the stream dies, restart live view instead of sitting there stalled."""
        while self.running:
            time.sleep(3)
            if not self.running:
                return
            if self.capturing or time.time() < self._recover_until:
                continue
            if time.time() - self._last_frame_time < 8:
                continue
            self._log("watchdog: no frames for >5s, restarting live view")
            try:
                r = self._start_live()
                self._log("watchdog: restart ack=%s"
                          % (r[1].hex(" ") if r else "-"))
            except Exception as e:
                self._log("watchdog: %s" % e)
            self._last_frame_time = time.time()      # back off before retrying

    def _reader(self):
        buf = b""
        epoch = -1
        while self.running:
            if epoch != self._epoch:
                epoch = self._epoch
                buf = b""                 # discard whatever the previous stream left
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
            frames, buf = ls.frames_from(buf)      # drops control packets for us
            for frame in frames:
                plen = int.from_bytes(frame[12:16], "little")
                payload = frame[24:24 + plen]
                if not self._synced:
                    # a decoder cannot start mid-GOP; wait for the SPS that opens one
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
            if self._rec_file:
                self._rec_file.write(payload)
            try:
                self.ff.stdin.write(payload)
                self.ff.stdin.flush()
            except Exception as e:
                self._log("writer: %s" % type(e).__name__)
                return

    def _decoder(self):
        while self.running:
            try:
                data = self.ff.stdout.read(self.frame_bytes)
            except Exception:
                return
            if not data or len(data) < self.frame_bytes:
                return
            self._frames_seen += 1
            with self.frames_lock:
                self.frames.append(data)
            self.times.append(time.time())
            self._last_frame_time = time.time()

    # ---- ui loop

    def _tick(self):
        if not self.running:
            return
        # rebuilding a 2 MB PhotoImage is expensive; only do it when a new frame
        # actually arrived, otherwise the event loop starves and clicks get missed
        if self._frames_seen == self._shown:
            self.root.after(8, self._tick)
            return
        with self.frames_lock:
            data = self.frames[-1] if self.frames else None
        if data:
            self._shown = self._frames_seen
            try:
                img = tk.PhotoImage(data=b"P6\n%d %d\n255\n" % (self.W, self.H) + data)
                self.view.configure(image=img, text="")
                self._img = img              # keep a reference or Tk drops the image
            except Exception as e:
                self._log("display error: %s" % e)
        self.root.after(12, self._tick)

    def _stats(self):
        if not self.running:
            return
        # camera I/O happens off the UI thread: if a USB call hangs while the
        # reconnect thread holds the lock, a blocking _stats would freeze the window
        threading.Thread(target=self._stats_read, daemon=True).start()
        self.root.after(2000, self._stats)

    def _stats_read(self):
        cutoff = time.time() - 2.0
        fps = sum(1 for t in self.times if t >= cutoff) / 2.0
        recovering = self.capturing or time.time() < self._recover_until
        stalled = (not recovering) and (time.time() - self._last_frame_time) > 3.0
        if self.disconnected:
            state = "camera disconnected - waiting"
        elif recovering:
            state = "recovering (camera writing)"
        else:
            state = "STALLED" if stalled else "live"

        mode = batt = None
        try:
            if self.cam is not None:
                with self.lock:
                    mode = self.cam.read(0x0043)
                    batt = self.cam.read(0x002f)
        except Exception as e:
            self._mark_lost("stats: %s" % type(e).__name__)

        text = "%dx%d   %.1f fps   %s   drops=%d   mode=%s   battery=%s%%" % (
            self.W, self.H, fps, state, self.drops,
            "video" if mode == 1 else ("photo" if mode == 0 else "?"),
            batt if batt is not None else "?")
        self.root.after(0, lambda t=text: self.status.configure(text=t))
        self._log("fps=%.1f frames=%d drops=%d state=%s"
                  % (fps, self._frames_seen, self.drops, state))

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
                self._stop_live()
        except Exception:
            pass
        if self._rec_file:
            self._rec_file.close()
        try:
            self.ff.kill()
        except Exception:
            pass
        try:
            self.cam.close()
        except Exception:
            pass
        self.root.destroy()


def main():
    scale = (960, 720)
    if "--scale" in sys.argv:
        w, h = sys.argv[sys.argv.index("--scale") + 1].lower().split("x")
        scale = (int(w), int(h))
    record = None
    if "--record" in sys.argv:
        record = sys.argv[sys.argv.index("--record") + 1]
    root = tk.Tk()
    App(root, scale, record)
    root.mainloop()


if __name__ == "__main__":
    main()
