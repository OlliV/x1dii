"""Primary scopes for the picture being looked at: an RGB parade waveform and a vectorscope.

They are computed from what is visible - the whole frame when it is fitted to the window,
only the part on screen when zoomed to 100% - so a zoom on a face measures the face.

  waveform     each channel in its own panel; across is across the visible area, up is
               the code value (0-100%), brightness is how many pixels sit there
  vectorscope  chroma (Cb right, Cr up, as the camera's JPEG encodes it); the angle is
               hue and the distance from the centre is saturation. Boxes mark 75% R, M,
               B, C, G, Y; the line through the upper left is the skin tone line.
               Z (or a click on it) zooms it 2x, as on Resolve's scope, to spread out
               the low-saturation colours skin and most real scenes are made of

numpy does the counting: both scopes are 2-D histograms, built with one bincount each.
"""
import math
import tkinter as tk

import numpy as np
from PIL import Image, ImageTk

import widgets as ui

W = 460                    # canvas
PAD = 12
HEAD = 22                  # section heading height
WAVE_H = 180               # waveform panels
LABEL_W = 30               # room for the 0..100 labels
VEC = 236                  # vectorscope square
TOP_GAP = 12               # between a heading and its scope
H = PAD + HEAD + TOP_GAP + WAVE_H + 14 + HEAD + VEC + PAD
SAMPLE_ROWS = 720          # rows sampled per waveform column
CHROMA_FULL = 140.0        # Cb/Cr distance at the scope's outer ring (100% primaries ~130)
CHANNELS = ((255, 70, 50), (60, 230, 70), (40, 140, 255))      # parade colours


def _bt601(rgb):
    """(Cb, Cr) offsets from 128 for an 8-bit RGB triple, as JPEG / Pillow encode them."""
    r, g, b = rgb
    return (-0.168736 * r - 0.331264 * g + 0.5 * b,
            0.5 * r - 0.418688 * g - 0.081312 * b)


TARGETS = (("R", (255, 0, 0)), ("M", (255, 0, 255)), ("B", (0, 0, 255)),
           ("C", (0, 255, 255)), ("G", (0, 255, 0)), ("Y", (255, 255, 0)))
SKIN_ANGLE = 123.0         # degrees from +Cb, the conventional skin tone (I) line


def waveform(img, panel_w, panel_h):
    """RGB parade of `img` as an image (panel_h, 3 * panel_w), one panel per channel."""
    small = img.resize((panel_w, SAMPLE_ROWS), Image.BOX)
    a = np.asarray(small, dtype=np.int32)                      # rows, cols, 3
    cols = np.broadcast_to(np.arange(panel_w), a.shape[:2])
    out = np.zeros((panel_h, panel_w * 3, 3), dtype=np.float32)
    for c, colour in enumerate(CHANNELS):
        # count at all 256 code values, then scale to the panel height by averaging:
        # binning straight into panel_h rows maps two code values to some rows and one
        # to others, which shows as horizontal stripes
        level = 255 - a[:, :, c]
        full = np.bincount((level * panel_w + cols).ravel(),
                           minlength=256 * panel_w).reshape(256, panel_w)
        counts = np.asarray(Image.fromarray(full.astype(np.float32), "F")
                            .resize((panel_w, panel_h), Image.BOX))
        v = np.log1p(counts) / math.log1p(max(1, counts.max()))
        v = np.clip(v * 1.35, 0, 1)
        tint = np.array(colour, dtype=np.float32)
        hot = v[..., None] ** 3 * (255 - tint) * 0.55        # dense spots run to white
        out[:, c * panel_w:(c + 1) * panel_w] = v[..., None] * tint + hot
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def _hue_map(size, full):
    """For each vectorscope pixel, the colour that chroma is at mid luma."""
    half = size / 2
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    cb = (x - half) / half * full
    cr = (half - y) / half * full
    luma = 150.0
    rgb = np.stack([luma + 1.402 * cr,
                    luma - 0.344136 * cb - 0.714136 * cr,
                    luma + 1.772 * cb], axis=-1)
    return np.clip(rgb, 40, 255)


_HUE = {}


def vectorscope(img, size, full=CHROMA_FULL):
    """Chroma plot of `img` as a (size, size) image on black; `full` is the Cb/Cr
    distance at the edge, so a smaller one magnifies."""
    small = img.resize((320, 240), Image.BOX)
    r, g, b = np.moveaxis(np.asarray(small, dtype=np.float32), -1, 0)
    # BT.601 in float: Pillow's YCbCr rounds to whole codes, which shows as a dot grid
    # once the scope is zoomed
    cb = -0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 0.5 * r - 0.418688 * g - 0.081312 * b
    half = size / 2
    fx = (half + cb / full * half).ravel()
    fy = (half - cr / full * half).ravel()
    # chroma beyond the edge (zoomed in) falls off the scope rather than piling up on
    # the border
    keep = (fx - half) ** 2 + (fy - half) ** 2 < half * half
    x = np.clip(fx[keep], 0, size - 1).astype(np.int32)
    y = np.clip(fy[keep], 0, size - 1).astype(np.int32)
    counts = np.bincount(y * size + x, minlength=size * size).reshape(size, size)
    v = np.log1p(counts) / math.log1p(max(1, counts.max()))
    v = np.clip(v * 1.5, 0, 1)[..., None]
    hue = _HUE.get((size, full))
    if hue is None:
        hue = _HUE[(size, full)] = _hue_map(size, full)
    out = hue * v + (255 - hue) * v ** 3 * 0.6
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


class Scopes:
    """A small window with both scopes. show(img, label) updates it, hide() hides it."""

    def __init__(self, root, geometry=None, on_geometry=None, log=None, vec_zoom=1,
                 on_vec_zoom=None):
        self.root = root
        self.vec_zoom = 2 if vec_zoom == 2 else 1
        self.on_vec_zoom = on_vec_zoom
        self.geometry = geometry
        self.on_geometry = on_geometry
        self.log = log or (lambda _msg: None)
        self.win = None
        self.canvas = None
        self.enabled = True           # the user can close it; S brings it back
        self._pending = None
        self._job = None
        self._geom_job = None
        self._photos = []
        self.f_head = ui.font(root, 10, "bold")
        self.f_small = ui.font(root, 9)

    # ---- window

    def _ensure_window(self):
        if self.win is not None and self.win.winfo_exists():
            if self.win.state() == "withdrawn":
                self.win.deiconify()
            return
        win = tk.Toplevel(self.root)
        win.title("Scopes")
        win.configure(bg="#000")
        win.resizable(False, False)
        if self.geometry:
            win.geometry(self.geometry)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Configure>", self._moved)
        c = tk.Canvas(win, width=W, height=H, bg="#000", highlightthickness=0, bd=0)
        c.pack()
        win.bind("z", lambda e: self.toggle_vec_zoom())
        c.tag_bind("vzoom", "<Button-1>", lambda e: self.toggle_vec_zoom())
        c.tag_bind("vzoom", "<Enter>", lambda e: c.configure(cursor="hand2"))
        c.tag_bind("vzoom", "<Leave>", lambda e: c.configure(cursor=""))
        self.win, self.canvas = win, c

    def close(self):
        """The user closed it: stay hidden until toggled back on."""
        self.enabled = False
        self.hide()

    def toggle(self):
        self.enabled = not self.enabled
        if self.enabled and self._pending is not None:
            self._render()
        elif not self.enabled:
            self.hide()

    def toggle_vec_zoom(self):
        """Vectorscope 1x <-> 2x."""
        self.vec_zoom = 1 if self.vec_zoom == 2 else 2
        if self.on_vec_zoom:
            self.on_vec_zoom(self.vec_zoom)
        if self.enabled and self._pending is not None:
            self._render()

    def hide(self):
        if self._job is not None:
            self.root.after_cancel(self._job)
            self._job = None
        if self.win is not None and self.win.winfo_exists():
            self.win.withdraw()

    def _moved(self, event):
        if event.widget is not self.win or not self.on_geometry:
            return
        if self._geom_job is not None:
            self.root.after_cancel(self._geom_job)
        self._geom_job = self.root.after(500, self._report_geometry)

    def _report_geometry(self):
        self._geom_job = None
        if self.win is not None and self.win.winfo_exists():
            geom = "+%d+%d" % (self.win.winfo_x(), self.win.winfo_y())
            if geom != self.geometry:
                self.geometry = geom
                self.on_geometry(geom)

    # ---- content

    def show(self, img, label):
        """Measure `img` (the visible area) once the view stops changing."""
        self._pending = (img, label)
        if not self.enabled:
            return
        if self._job is not None:
            self.root.after_cancel(self._job)
        self._job = self.root.after(80, self._render)

    def _render(self):
        self._job = None
        if self._pending is None:
            return
        img, label = self._pending
        self._ensure_window()
        c = self.canvas
        c.delete("all")
        self._photos = []
        try:
            self._draw_waveform(img)
            self._draw_vectorscope(img, label)
        except Exception as e:                    # a scope must never take the app down
            self.log("scopes: %s: %s" % (type(e).__name__, e))

    def _heading(self, y, text, right=None):
        c = self.canvas
        c.create_text(PAD, y + HEAD / 2, text=text, anchor="w", font=self.f_head,
                      fill=ui.MUTED)
        if right:
            c.create_text(W - PAD, y + HEAD / 2, text=right, anchor="e", font=self.f_small,
                          fill=ui.DIM)

    def _draw_waveform(self, img):
        c = self.canvas
        top = PAD
        self._heading(top, "WAVEFORM  ·  RGB PARADE")
        y0 = top + HEAD + TOP_GAP
        x0 = PAD + LABEL_W
        gap = 6
        panel_w = (W - x0 - PAD - 2 * gap) // 3
        wave = waveform(img, panel_w, WAVE_H)
        for ch in range(3):
            part = wave.crop((ch * panel_w, 0, (ch + 1) * panel_w, WAVE_H))
            photo = ImageTk.PhotoImage(part, master=self.root)
            self._photos.append(photo)
            c.create_image(x0 + ch * (panel_w + gap), y0, image=photo, anchor="nw")
        right = x0 + 3 * panel_w + 2 * gap
        for pct in (0, 25, 50, 75, 100):
            y = y0 + (WAVE_H - 1) * (1 - pct / 100.0)
            c.create_line(x0, y, right, y, fill="#3a3a3e" if pct in (0, 100) else "#232326")
            c.create_text(x0 - 6, y, text=str(pct), anchor="e", font=self.f_small,
                          fill=ui.DIM)

    def _draw_vectorscope(self, img, label):
        c = self.canvas
        top = PAD + HEAD + TOP_GAP + WAVE_H + 14
        self._heading(top, "VECTORSCOPE", right=label)
        self._zoom_button(top)
        size = VEC
        x0 = (W - size) / 2
        y0 = top + HEAD
        full = CHROMA_FULL / self.vec_zoom
        photo = ImageTk.PhotoImage(vectorscope(img, size, full), master=self.root)
        self._photos.append(photo)
        c.create_image(x0, y0, image=photo, anchor="nw", tags="vzoom")
        cx, cy, half = x0 + size / 2, y0 + size / 2, size / 2

        # hue ring, as on a hardware scope
        ring = half - 2
        for k in range(72):
            ang = k * 5.0
            cb, cr = math.cos(math.radians(ang + 2.5)), math.sin(math.radians(ang + 2.5))
            col = _ring_colour(cb, cr)
            c.create_arc(cx - ring, cy - ring, cx + ring, cy + ring, start=ang, extent=5.2,
                         style="arc", outline=col, width=3)
        for frac in (0.25, 0.5, 0.75, 1.0):
            r = (half - 8) * frac
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#26262a")
        c.create_line(cx - half + 8, cy, cx + half - 8, cy, fill="#26262a")
        c.create_line(cx, cy - half + 8, cx, cy + half - 8, fill="#26262a")

        # skin tone line
        a = math.radians(SKIN_ANGLE)
        c.create_line(cx, cy, cx + math.cos(a) * (half - 8), cy - math.sin(a) * (half - 8),
                      fill="#8a7a6a", dash=(3, 3))

        # 75% targets; zoomed in they lie beyond the edge, so only the direction shows,
        # as a tick on the hue ring
        for name, rgb in TARGETS:
            cb, cr = _bt601([v * 0.75 for v in rgb])
            tx = cx + cb / full * half
            ty = cy - cr / full * half
            dx, dy = tx - cx, ty - cy
            n = math.hypot(dx, dy) or 1
            if n > half - 12:
                ux, uy = dx / n, dy / n
                c.create_line(cx + ux * (half - 14), cy + uy * (half - 14),
                              cx + ux * (half - 6), cy + uy * (half - 6), fill="#8e8e93",
                              width=2)
                c.create_text(cx + ux * (half - 22), cy + uy * (half - 22), text=name,
                              font=self.f_small, fill="#8e8e93")
                continue
            c.create_rectangle(tx - 5, ty - 5, tx + 5, ty + 5, outline="#8e8e93")
            c.create_text(tx + dx / n * 14, ty + dy / n * 14, text=name,
                          font=self.f_small, fill="#8e8e93")

    def _zoom_button(self, top):
        """A 1x / 2x pill after the heading; clicking it (or the scope) toggles."""
        c = self.canvas
        x = PAD + self.f_head.measure("VECTORSCOPE") + 10
        w, h = 30, 16
        y = top + (HEAD - h) / 2
        on = self.vec_zoom == 2
        ui.rounded_rect(c, x, y, x + w, y + h, 8, fill=ui.ACCENT if on else ui.TILE,
                        outline="" if on else ui.TILE_EDGE, tags="vzoom")
        c.create_text(x + w / 2, y + h / 2, text="2\u00d7" if on else "1\u00d7",
                      font=self.f_small, fill="#000" if on else ui.MUTED, tags="vzoom")


def _ring_colour(cb, cr):
    """Hex colour of the hue at unit chroma direction (cb, cr)."""
    k = 120.0
    y = 140.0
    r = y + 1.402 * cr * k
    g = y - 0.344136 * cb * k - 0.714136 * cr * k
    b = y + 1.772 * cb * k
    m = max(r, g, b, 1)
    scale = 255.0 / m if m > 255 else 1.0
    return "#%02x%02x%02x" % tuple(int(max(0, min(255, v * scale))) for v in (r, g, b))
