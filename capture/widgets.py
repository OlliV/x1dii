"""Dark, camera-style controls for the tether window, drawn on Tk canvases.

Aqua ignores most styling on stock widgets: menubuttons and checkbuttons keep their light
system faces whatever colours they are given, which is what made the panel look unfinished
next to the drawn release button. These draw themselves instead and use the native menu
only for the drop-down list, where native is what a Mac user expects.

  ValueTile   a labelled value ("APERTURE / f/4") that opens a menu of choices
  Switch      a pill toggle with a label, bound to a BooleanVar
  Chip        a small clickable text button (the save folder)
"""
import tkinter as tk
import tkinter.font as tkfont

# palette - neutral greys a step apart, one accent shared with the release button
BG = "#111111"            # window
TILE = "#1e1e20"          # resting surface
TILE_HOVER = "#2a2a2d"
TILE_EDGE = "#2f2f33"
TEXT = "#f2f2f2"
MUTED = "#8e8e93"         # labels, hints
DIM = "#4c4c50"           # disabled text
ACCENT = "#F57513"        # Hasselblad orange, as on the release
SWITCH_OFF = "#3a3a3d"

_SYSTEM = ".AppleSystemUIFont"     # SF on macOS; Tk resolves it though it is not listed


def font(root, size, weight="normal"):
    """The system font at `size` points, falling back to Helvetica Neue elsewhere."""
    family = _SYSTEM if root.tk.call("tk", "windowingsystem") == "aqua" else "Helvetica Neue"
    return tkfont.Font(root=root, family=family, size=size, weight=weight)


def rounded_rect(canvas, x0, y0, x1, y1, r, **kw):
    """A rounded rectangle as one smoothed polygon (canvas has no native one)."""
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return canvas.create_polygon(pts, smooth=True, splinesteps=24, **kw)


class _Drawn(tk.Canvas):
    """Shared plumbing: background, hover, enabled state, pointer cursor."""

    def __init__(self, parent, width, height, bg=BG):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._hover = False
        self._enabled = True
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", self._click)

    @property
    def enabled(self):
        return self._enabled

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled != self._enabled:
            self._enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
            self.redraw()

    def _set_hover(self, on):
        self._hover = on
        self.redraw()

    def _click(self, _event):
        if self._enabled:
            self.activate()

    def activate(self):
        pass

    def redraw(self):
        pass


class ValueTile(_Drawn):
    """A setting shown as a tile; a click opens the list of values.

    `variable` holds the chosen text, exactly as before with tk.OptionMenu, so callers
    keep reading and setting it. `display` turns a choice into what the tile shows
    (e.g. "4" -> "f/4"). When disabled the tile keeps showing the value, dimmed, with
    `disabled_note` beside the label - the camera is choosing it, not the user.
    """

    def __init__(self, parent, label, variable, command=None, width=118, height=58,
                 display=None, bg=BG, value_size=19, disabled_note="AUTO"):
        super().__init__(parent, width, height, bg=bg)
        self.label = label.upper()
        self.variable = variable
        self.command = command
        self.display = display or (lambda v: v)
        self.disabled_note = disabled_note
        self.w, self.h = width, height
        root = self.winfo_toplevel()
        self.f_label = font(root, 10, "bold")
        self.f_value = font(root, value_size)
        self.f_note = font(root, 8, "bold")
        self.menu = tk.Menu(self, tearoff=False)
        self._choices = []
        variable.trace_add("write", lambda *_: self.redraw())
        self.redraw()

    def set_choices(self, choices):
        """Replace the menu. Each entry is a radio item, so the current one is ticked."""
        self._choices = list(choices)
        self.menu.delete(0, "end")
        for choice in self._choices:
            self.menu.add_radiobutton(label=str(choice), value=str(choice),
                                      variable=self.variable,
                                      command=lambda c=choice: self._picked(c))

    def _picked(self, choice):
        if self.command:
            self.command(choice)

    def activate(self):
        if not self._choices:
            return
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.h + 2
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    def redraw(self):
        self.delete("all")
        on, hot = self._enabled, self._hover and self._enabled
        rounded_rect(self, 1, 1, self.w - 1, self.h - 1, 10,
                     fill=TILE_HOVER if hot else TILE,
                     outline=TILE_EDGE if on else TILE, width=1)
        ly, vy = 14, 15 + (self.h - 15) / 2 + 2      # label line, value line
        self.create_text(12, ly, text=self.label, anchor="w", font=self.f_label,
                         fill=MUTED if on else DIM)
        value = self.variable.get()
        text = self.display(value) if value not in ("", None) else "--"
        self.create_text(12, vy, text=text, anchor="w", font=self.f_value,
                         fill=TEXT if on else DIM)
        if not on and self.disabled_note and self._choices:
            self.create_text(self.w - 10, vy, text=self.disabled_note, anchor="e",
                             font=self.f_note, fill=ACCENT)
        elif on and self._choices:          # a small chevron says "this opens"
            cx, cy = self.w - 15, vy
            self.create_line(cx - 4, cy - 2, cx, cy + 2, cx + 4, cy - 2,
                             fill=TEXT if hot else MUTED, width=1.6,
                             capstyle="round", joinstyle="round")


class Switch(_Drawn):
    """A pill switch with its label to the right; the whole thing is clickable."""

    def __init__(self, parent, text, variable, command=None, bg=BG, size=12,
                 width=None):
        root = parent.winfo_toplevel()
        self.f_text = font(root, size)
        self.text = text
        self.variable = variable
        self.command = command
        self.tw, self.th = 30, 18                       # the pill
        width = width or self.tw + 9 + self.f_text.measure(text) + 2
        super().__init__(parent, width, 22, bg=bg)
        self.w = width
        variable.trace_add("write", lambda *_: self.redraw())
        self.redraw()

    def activate(self):
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()

    def redraw(self):
        self.delete("all")
        on, en = bool(self.variable.get()), self._enabled
        y0 = (22 - self.th) / 2
        track = (ACCENT if on else SWITCH_OFF) if en else "#2a2a2c"
        if on and self._hover and en:
            track = "#ff8a33"
        r = self.th / 2
        self.create_oval(1, y0, 1 + self.th, y0 + self.th, fill=track, outline="")
        self.create_oval(self.tw - self.th, y0, self.tw, y0 + self.th, fill=track,
                         outline="")
        self.create_rectangle(1 + r, y0, self.tw - r, y0 + self.th, fill=track,
                              outline="")
        d = self.th - 4
        kx = self.tw - d - 2 if on else 3
        self.create_oval(kx, y0 + 2, kx + d, y0 + 2 + d,
                         fill="#ffffff" if en else "#77777a", outline="")
        self.create_text(self.tw + 9, 11, text=self.text, anchor="w", font=self.f_text,
                         fill=(TEXT if self._hover else "#d8d8d8") if en else DIM)


class Chip(_Drawn):
    """A quiet button: a small glyph ("folder" or "grid") and a name, brighter on hover,
    dimmed when disabled."""

    def __init__(self, parent, text, command, bg=BG, size=11, max_width=150, glyph="folder"):
        root = parent.winfo_toplevel()
        self.f_text = font(root, size)
        self.command = command
        self.glyph = glyph
        self.max_width = max_width
        super().__init__(parent, max_width, 22, bg=bg)
        self.set_text(text)

    def set_text(self, text):
        self.text = text
        self.redraw()

    def activate(self):
        self.command()

    def _fit(self, text, room):
        """Shorten with an ellipsis so a long folder name cannot widen the window."""
        if self.f_text.measure(text) <= room:
            return text
        while text and self.f_text.measure(text + "\u2026") > room:
            text = text[:-1]
        return text + "\u2026"

    def redraw(self):
        self.delete("all")
        hot = self._hover and self._enabled
        label = self._fit(self.text, self.max_width - 36)
        tw = self.f_text.measure(label)
        rounded_rect(self, 1, 1, tw + 35, 21, 8, fill=TILE_HOVER if hot else TILE,
                     outline=TILE_EDGE)
        ink = TEXT if hot else MUTED if self._enabled else DIM
        if self.glyph == "grid":                # four small frames, as in the gallery
            for gx, gy in ((9, 6), (16, 6), (9, 12), (16, 12)):
                self.create_rectangle(gx, gy, gx + 5, gy + 4, outline=ink, width=1.1)
        else:                                   # folder: a tab and a body
            self.create_polygon(9, 7, 13, 7, 15, 9, 22, 9, 22, 16, 9, 16, fill="",
                                outline=ink, width=1.2, joinstyle="round")
        self.create_text(28, 11, text=label, anchor="w", font=self.f_text,
                         fill=TEXT if hot else "#c8c8cc" if self._enabled else DIM)
