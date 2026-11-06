"""The last four captures, for shooting with live view off.

A dark window where the live preview would otherwise sit, as large as fits below the
controls and freely resizable. It shows the four most recent captures in a 2x2 grid,
newest top-left; clicking one fills the window with it and clicking again goes back to
the grid. Esc also goes back, 1-4 open a capture, and space or return fire the shutter as
they do in the controls window.

Each capture's embedded JPEG is decoded once (decode(), on a worker) and kept in memory;
the window resizes those with Pillow to whatever size it currently is.
"""
import io
import time
import tkinter as tk

from PIL import Image, ImageTk

import widgets as ui

SLOTS = 4
GAP = 3                   # between cells
ASPECT = 4 / 3            # the X1D II sensor, 8272 x 6200
KEEP_EDGE = 3000          # decoded size kept per capture; plenty for any screen


def decode(jpeg):
    """JPEG bytes -> an RGB image no larger than KEEP_EDGE. Safe on a worker thread.

    draft() lets the JPEG decoder scale down by a power of two while decoding, which is
    much faster than decoding the full frame and shrinking it afterwards.
    """
    img = Image.open(io.BytesIO(jpeg))
    img.draft("RGB", (KEEP_EDGE, KEEP_EDGE))
    img = img.convert("RGB")
    img.thumbnail((KEEP_EDGE, KEEP_EDGE), Image.LANCZOS)
    return img


def fit(img, box):
    """`img` scaled to fit inside `box` (w, h), keeping its aspect ratio."""
    bw, bh = max(1, int(box[0])), max(1, int(box[1]))
    scale = min(bw / img.width, bh / img.height)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.LANCZOS)


class Gallery:
    def __init__(self, root, place, on_capture=None, log=None, geometry=None,
                 on_geometry=None):
        """`place` is (left, top, room_w, room_h): where the window goes and the screen
        space it may fill. `geometry` ("WxH+X+Y") overrides it with where the user left
        the window last time; `on_geometry` is told whenever they move or resize it."""
        self.root = root
        self.on_capture = on_capture
        self.geometry = geometry
        self.on_geometry = on_geometry
        self._geom_job = None
        self.log = log or (lambda _msg: None)
        left, top, room_w, room_h = (int(v) for v in place)
        self.h = int(min(room_h, room_w / ASPECT))
        self.w = int(round(self.h * ASPECT))
        self.left, self.top = left, top
        self.items = []               # newest first: dict(image, title, when, cache)
        self.single = None            # index shown full, or None for the grid
        self.hover = None             # grid cell under the pointer
        self.win = None
        self.canvas = None
        self._images = []             # PhotoImage references, or Tk drops the pixels
        self._resize_job = None
        self.f_title = ui.font(root, 11, "bold")
        self.f_small = ui.font(root, 10)
        self.f_tag = ui.font(root, 9, "bold")

    @property
    def cell_size(self):
        return (self.w - GAP) // 2, (self.h - GAP) // 2

    # ---- content

    def add(self, image, title):
        """UI thread: a new capture (from decode()). Shows the grid, newest first."""
        self.items.insert(0, {"image": image, "title": title,
                              "when": time.strftime("%H:%M:%S"), "cache": {}})
        del self.items[SLOTS:]
        self.single = None
        self._ensure_window()
        self.redraw()

    def _photo(self, item, box):
        """A PhotoImage of `item` fitted to `box`, made once per size."""
        key = (int(box[0]), int(box[1]))
        photo = item["cache"].get(key)
        if photo is None:
            item["cache"].clear()                 # one size per item is all that is shown
            photo = ImageTk.PhotoImage(fit(item["image"], key), master=self.root)
            item["cache"][key] = photo
        return photo

    # ---- window

    def _ensure_window(self):
        if self.win is not None and self.win.winfo_exists():
            if self.win.state() == "withdrawn":
                self.win.deiconify()
            self.win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("X1D II captures")
        win.configure(bg="#000")
        win.minsize(320, 240)
        win.geometry(self.geometry or
                     "%dx%d+%d+%d" % (self.w, self.h, self.left, self.top))
        win.protocol("WM_DELETE_WINDOW", win.withdraw)     # keep the captures
        c = tk.Canvas(win, width=self.w, height=self.h, bg="#000",
                      highlightthickness=0, bd=0)
        c.pack(fill="both", expand=True)
        c.bind("<Configure>", self._configure)
        win.bind("<Configure>", self._moved)
        c.bind("<Button-1>", self._click)
        c.bind("<Motion>", self._motion)
        c.bind("<Leave>", lambda e: self._set_hover(None))
        win.bind("<Escape>", lambda e: self.show_grid())
        for n in range(1, SLOTS + 1):
            win.bind(str(n), lambda e, i=n - 1: self.show_single(i))
        if self.on_capture:
            win.bind("<space>", lambda e: self.on_capture())
            win.bind("<Return>", lambda e: self.on_capture())
        self.win, self.canvas = win, c

    def _configure(self, event):
        """Window resized: redraw once the drag pauses, at the new size."""
        if (event.width, event.height) == (self.w, self.h):
            return
        self.w, self.h = event.width, event.height
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(120, self._resized)

    def _moved(self, event):
        """Moved or resized: report the geometry once it settles, to be remembered."""
        if event.widget is not self.win or not self.on_geometry:
            return
        if self._geom_job is not None:
            self.root.after_cancel(self._geom_job)
        self._geom_job = self.root.after(500, self._report_geometry)

    def _report_geometry(self):
        self._geom_job = None
        if self.win is not None and self.win.winfo_exists():
            geom = self.win.geometry()
            if geom != self.geometry:
                self.geometry = geom
                self.on_geometry(geom)

    def _resized(self):
        self._resize_job = None
        self.redraw()

    def show_grid(self):
        if self.single is not None:
            self.single = None
            self.redraw()

    def show_single(self, index):
        if 0 <= index < len(self.items):
            self.single = index
            self.hover = None
            self.redraw()

    # ---- input

    def _cell_at(self, x, y):
        cw, ch = self.cell_size
        col = 0 if x < cw + GAP / 2 else 1
        row = 0 if y < ch + GAP / 2 else 1
        return row * 2 + col

    def _click(self, event):
        if self.single is not None:
            self.show_grid()
            return
        self.show_single(self._cell_at(event.x, event.y))

    def _motion(self, event):
        if self.single is None:
            i = self._cell_at(event.x, event.y)
            self._set_hover(i if i < len(self.items) else None)

    def _set_hover(self, index):
        if index != self.hover:
            self.hover = index
            if self.canvas is not None:
                self.canvas.configure(cursor="hand2" if index is not None else "arrow")
            self.redraw()

    # ---- drawing

    def redraw(self):
        c = self.canvas
        if c is None:
            return
        c.delete("all")
        self._images = []
        if self.single is not None and self.single < len(self.items):
            self._draw_single(self.items[self.single])
        else:
            for slot in range(SLOTS):
                self._draw_cell(slot)

    def _caption(self, x, y, item, latest=False):
        """Name and time on a dark pill, anchored at its bottom-left corner."""
        c = self.canvas
        title, when = item["title"], item["when"]
        tw = self.f_title.measure(title)
        ww = self.f_small.measure(when)
        tag_w = self.f_tag.measure("LATEST") + 12 if latest else 0
        w = 12 + tw + 10 + ww + 12 + (tag_w + 8 if latest else 0)
        ui.rounded_rect(c, x, y - 26, x + w, y, 9, fill="#0b0b0c", outline="")
        cx = x + 12
        if latest:
            ui.rounded_rect(c, cx - 4, y - 21, cx - 4 + tag_w, y - 5, 6,
                            fill=ui.ACCENT, outline="")
            c.create_text(cx - 4 + tag_w / 2, y - 13, text="LATEST", font=self.f_tag,
                          fill="#ffffff")
            cx += tag_w + 4
        c.create_text(cx, y - 13, text=title, anchor="w", font=self.f_title,
                      fill=ui.TEXT)
        c.create_text(cx + tw + 10, y - 13, text=when, anchor="w", font=self.f_small,
                      fill=ui.MUTED)

    def _draw_cell(self, slot):
        c = self.canvas
        cw, ch = self.cell_size
        x0 = (slot % 2) * (cw + GAP)
        y0 = (slot // 2) * (ch + GAP)
        c.create_rectangle(x0, y0, x0 + cw, y0 + ch, fill="#0c0c0d", outline="")
        if slot >= len(self.items):
            c.create_text(x0 + cw / 2, y0 + ch / 2, text="no capture yet",
                          font=self.f_small, fill=ui.DIM)
            return
        item = self.items[slot]
        photo = self._photo(item, (cw, ch))
        c.create_image(x0 + cw / 2, y0 + ch / 2, image=photo)
        self._images.append(photo)
        self._caption(x0 + 10, y0 + ch - 10, item, latest=(slot == 0))
        c.create_text(x0 + cw - 12, y0 + 16, text=str(slot + 1), anchor="e",
                      font=self.f_tag, fill=ui.MUTED)
        if slot == self.hover:
            c.create_rectangle(x0 + 1, y0 + 1, x0 + cw - 1, y0 + ch - 1,
                               outline=ui.ACCENT, width=2)

    def _draw_single(self, item):
        c = self.canvas
        photo = self._photo(item, (self.w, self.h))
        c.create_image(self.w / 2, self.h / 2, image=photo)
        self._images.append(photo)
        self._caption(14, self.h - 14, item, latest=(self.single == 0))
        hint = "click or Esc for all four"
        hw = self.f_small.measure(hint)
        ui.rounded_rect(c, self.w - hw - 34, 12, self.w - 12, 36, 9, fill="#0b0b0c",
                        outline="")
        c.create_text(self.w - 23, 24, text=hint, anchor="e", font=self.f_small,
                      fill=ui.MUTED)
