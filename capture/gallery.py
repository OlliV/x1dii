"""The last four captures, for shooting with live view off.

A dark window where the live preview would otherwise sit, as large as fits below the
controls and freely resizable. It shows the four most recent captures in a 2x2 grid,
newest top-left. Clicking one fills the window with it; clicking a spot there zooms to
100% around it, to judge focus, and a click (without dragging) goes back to fitting the
window. While zoomed, drag or two-finger scroll to look around; a small overview in the
corner shows where. Esc goes back to the grid, 1-4 open a capture, and space or return
fire the shutter as they do in the controls window.

Each capture's embedded JPEG (4136 x 3100, half the sensor in each direction) is decoded
once (decode(), on a worker) and kept in memory at full size, with a smaller copy for the
grid and the fitted view; the window resizes those with Pillow as it needs.
"""
import io
import time
import tkinter as tk

from PIL import Image, ImageTk

import widgets as ui

SLOTS = 4
GAP = 3                   # between cells
ASPECT = 4 / 3            # the X1D II sensor, 8272 x 6200
VIEW_EDGE = 1800          # the copy used for the grid and the fitted view
NAV_EDGE = 150            # the overview shown while zoomed
DRAG = 4                  # pointer travel, in points, that makes a press a drag


def decode(jpeg):
    """JPEG bytes -> (full, view): the preview at full size and a copy no larger than
    VIEW_EDGE for drawing it small. Safe on a worker thread."""
    full = Image.open(io.BytesIO(jpeg)).convert("RGB")
    view = full.copy()
    view.thumbnail((VIEW_EDGE, VIEW_EDGE), Image.LANCZOS)
    return full, view


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
        self.zoom = None              # (cx, cy) in full-size pixels while at 100%
        self._press = None            # (x, y, zoom-at-press) for click-or-drag
        self._dragged = False
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

    def add(self, decoded, title):
        """UI thread: a new capture (decode()'s result). Shows the grid, newest first."""
        full, view = decoded
        self.items.insert(0, {"full": full, "view": view, "title": title,
                              "when": time.strftime("%H:%M:%S"), "cache": {}})
        del self.items[SLOTS:]
        self.single = None
        self.zoom = None
        self._ensure_window()
        self.redraw()

    def _photo(self, item, box):
        """A PhotoImage of `item` fitted to `box`, made once per size."""
        key = (int(box[0]), int(box[1]))
        photo = item["cache"].get(key)
        if photo is None:
            item["cache"].clear()                 # one size per item is all that is shown
            view = item["view"]
            src = view if key[0] <= view.width and key[1] <= view.height else item["full"]
            photo = ImageTk.PhotoImage(fit(src, key), master=self.root)
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
        c.bind("<ButtonPress-1>", self._press_at)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Motion>", self._motion)
        c.bind("<MouseWheel>", lambda e: self._scroll(0, e.delta))
        c.bind("<Shift-MouseWheel>", lambda e: self._scroll(e.delta, 0))
        c.bind("<TouchpadScroll>", self._touchpad)
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
            self.zoom = None
            self._set_cursor()
            self.redraw()

    def show_single(self, index):
        if 0 <= index < len(self.items):
            self.single = index
            self.hover = None
            self.zoom = None
            self._set_cursor()
            self.redraw()

    # ---- input

    def _cell_at(self, x, y):
        cw, ch = self.cell_size
        col = 0 if x < cw + GAP / 2 else 1
        row = 0 if y < ch + GAP / 2 else 1
        return row * 2 + col

    def _press_at(self, event):
        self._press = (event.x, event.y, self.zoom)
        self._dragged = False

    def _drag(self, event):
        """While zoomed, the image follows the pointer, as a hand would move it."""
        if self._press is None or self._press[2] is None or self.zoom is None:
            return
        x0, y0, (cx0, cy0) = self._press
        dx, dy = event.x - x0, event.y - y0
        if not self._dragged and abs(dx) + abs(dy) < DRAG:
            return
        if not self._dragged:
            self._dragged = True
            self._set_cursor("closedhand")
        self._pan_to(cx0 - dx, cy0 - dy)

    def _release(self, event):
        press, self._press = self._press, None
        if press is None:
            return
        if self._dragged:
            self._dragged = False
            self._set_cursor()
            return
        if self.single is None:
            self.show_single(self._cell_at(event.x, event.y))
        elif self.zoom is None:
            self._zoom_at(event.x, event.y)
        else:
            self.zoom = None                   # a plain click while zoomed: fit again
            self._set_cursor()
            self.redraw()

    def _fit_rect(self, item):
        """Where the fitted image sits in the window: (x0, y0, scale) of the full image."""
        full = item["full"]
        scale = min(self.w / full.width, self.h / full.height)
        return ((self.w - full.width * scale) / 2, (self.h - full.height * scale) / 2,
                scale)

    def _zoom_at(self, x, y):
        """Zoom to 100% so the clicked spot stays under the pointer."""
        item = self.items[self.single]
        x0, y0, scale = self._fit_rect(item)
        px, py = (x - x0) / scale, (y - y0) / scale        # the spot, in full pixels
        self.zoom = (0, 0)
        self._pan_to(px - (x - self.w / 2), py - (y - self.h / 2), draw=False)
        self._set_cursor()
        self.redraw()

    def _pan_to(self, cx, cy, draw=True):
        """Centre the 100% view on (cx, cy), kept so the image never leaves the window."""
        full = self.items[self.single]["full"]
        half_w, half_h = self.w / 2, self.h / 2
        cx = full.width / 2 if full.width <= self.w else \
            min(max(cx, half_w), full.width - half_w)
        cy = full.height / 2 if full.height <= self.h else \
            min(max(cy, half_h), full.height - half_h)
        if (cx, cy) != self.zoom:
            self.zoom = (cx, cy)
            if draw:
                self.redraw()

    def _scroll(self, dx, dy):
        """A mouse wheel: pan while zoomed (Shift for sideways)."""
        if self.zoom is not None:
            self._pan_to(self.zoom[0] - dx * 4, self.zoom[1] - dy * 4)

    def _touchpad(self, event):
        """Two-finger scrolling on a trackpad pans while zoomed."""
        if self.zoom is None:
            return
        try:
            dx, dy = self.root.tk.splitlist(
                self.root.tk.call("tk::PreciseScrollDeltas", event.delta))
        except tk.TclError:
            return
        self._pan_to(self.zoom[0] - float(dx), self.zoom[1] - float(dy))

    def _set_cursor(self, name=None):
        if self.canvas is None:
            return
        if name is None:
            if self.single is None:
                name = "hand2" if self.hover is not None else "arrow"
            else:
                name = "openhand" if self.zoom is not None else "zoom-in"
        try:
            self.canvas.configure(cursor=name)
        except tk.TclError:
            self.canvas.configure(cursor="arrow")

    def _motion(self, event):
        if self.single is None:
            i = self._cell_at(event.x, event.y)
            self._set_hover(i if i < len(self.items) else None)

    def _set_hover(self, index):
        if index != self.hover:
            self.hover = index
            self._set_cursor()
            self.redraw()

    # ---- drawing

    def redraw(self):
        c = self.canvas
        if c is None:
            return
        c.delete("all")
        self._images = []
        if self.single is not None and self.single < len(self.items):
            if self.zoom is not None:
                self._draw_zoomed(self.items[self.single])
            else:
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
        self._hint("click to zoom to 100%  \u00b7  Esc for all four")

    def _hint(self, text):
        c = self.canvas
        hw = self.f_small.measure(text)
        ui.rounded_rect(c, self.w - hw - 34, 12, self.w - 12, 36, 9, fill="#0b0b0c",
                        outline="")
        c.create_text(self.w - 23, 24, text=text, anchor="e", font=self.f_small,
                      fill=ui.MUTED)

    def _draw_zoomed(self, item):
        """The full-size preview at 100%, only the part that is in the window."""
        c = self.canvas
        full = item["full"]
        cx, cy = self.zoom
        left = int(round(cx - self.w / 2))
        top = int(round(cy - self.h / 2))
        box = (max(0, left), max(0, top),
               min(full.width, left + self.w), min(full.height, top + self.h))
        photo = ImageTk.PhotoImage(full.crop(box), master=self.root)
        c.create_image(box[0] - left, box[1] - top, image=photo, anchor="nw")
        self._images.append(photo)
        self._navigator(item, box)
        self._caption(14, self.h - 14, item, latest=(self.single == 0))
        self._hint("100%  \u00b7  drag to look around  \u00b7  click to fit")

    def _navigator(self, item, box):
        """A small overview with the visible area outlined, top-left."""
        c = self.canvas
        nav = item["cache"].get("nav")
        if nav is None:
            small = item["view"].copy()
            small.thumbnail((NAV_EDGE, NAV_EDGE), Image.LANCZOS)
            nav = (ImageTk.PhotoImage(small, master=self.root), small.width, small.height)
            item["cache"]["nav"] = nav
        photo, nw, nh = nav
        x0, y0 = 14, 14
        c.create_rectangle(x0 - 3, y0 - 3, x0 + nw + 3, y0 + nh + 3, fill="#0b0b0c",
                           outline="")
        c.create_image(x0, y0, image=photo, anchor="nw")
        self._images.append(photo)
        k = nw / item["full"].width
        c.create_rectangle(x0 + box[0] * k, y0 + box[1] * k, x0 + box[2] * k,
                           y0 + box[3] * k, outline=ui.ACCENT, width=1.5)
