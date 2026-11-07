"""Dark, camera-style controls for the tether window, drawn on Tk canvases.

Aqua ignores most styling on stock widgets: menubuttons and checkbuttons keep their light
system faces whatever colours they are given, which is what made the panel look unfinished
next to the drawn release button. These draw themselves instead and use the native menu
only for the drop-down list, where native is what a Mac user expects.

  ValueTile   a labelled value ("APERTURE / f/4") that opens a menu of choices
  StackTile   the focus stack as one tile: on/off, frames, step and direction in its menu
  LensTile    the attached lens and where it is focused, read only
  ModeBadge   the exposure mode as the dial marks it, a video camera for video
  Switch      a pill toggle with a label, bound to a BooleanVar
  Chip        a small clickable text button (the save folder)

The look follows Hasselblad's own: near-black surfaces a step apart, hairline edges,
small letter-spaced capitals for labels, and orange only for the release and for what
is switched on.
"""
import tkinter as tk
import tkinter.font as tkfont

# palette - near-black greys a step apart, one accent shared with the release button
BG = "#0c0c0d"            # window
TILE = "#18181a"          # resting surface
TILE_HOVER = "#222225"
TILE_EDGE = "#29292c"     # hairlines
TEXT = "#f4f4f4"
MUTED = "#86868b"         # labels, hints
DIM = "#45454a"           # disabled text
ACCENT = "#F57513"        # Hasselblad orange, as on the release
SWITCH_OFF = "#333336"
TRACK = 1.4               # letter spacing of tile labels, in points

_SYSTEM = ".AppleSystemUIFont"     # SF on macOS; Tk resolves it though it is not listed


def font(root, size, weight="normal"):
    """The system font at `size` points, falling back to Helvetica Neue elsewhere."""
    family = _SYSTEM if root.tk.call("tk", "windowingsystem") == "aqua" else "Helvetica Neue"
    return tkfont.Font(root=root, family=family, size=size, weight=weight)


def tracked_width(fnt, text, track=TRACK):
    """The width of `text` drawn by tracked_text()."""
    return sum(fnt.measure(ch) for ch in text) + track * max(0, len(text) - 1)


def tracked_text(canvas, x, y, text, fnt, fill, track=TRACK):
    """Letter-spaced text, left-anchored at (x, y): Tk cannot track a font, so each
    character is placed on its own. Used for the small capitals of tile labels."""
    for ch in text:
        canvas.create_text(x, y, text=ch, anchor="w", font=fnt, fill=fill)
        x += fnt.measure(ch) + track
    return x


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
        self.f_label = font(root, 9, "bold")
        self.f_value = font(root, value_size)
        self.f_value_small = font(root, value_size - 4)     # when the value would not fit
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
        rounded_rect(self, 1, 1, self.w - 1, self.h - 1, 8,
                     fill=TILE_HOVER if hot else TILE,
                     outline=TILE_EDGE if on else TILE, width=1)
        ly, vy = 14, 15 + (self.h - 15) / 2 + 2      # label line, value line
        tracked_text(self, 12, ly, self.label, self.f_label, MUTED if on else DIM)
        value = self.variable.get()
        text = self.display(value) if value not in ("", None) else "--"
        # what sits at the right of the value line: the AUTO note, or the chevron
        noted = not on and self.disabled_note and self._choices
        right = (self.w - 10 - self.f_note.measure(self.disabled_note) if noted
                 else self.w - 22 if on and self._choices else self.w - 10)
        f = self.f_value if 12 + self.f_value.measure(text) + 6 <= right \
            else self.f_value_small
        self.create_text(12, vy, text=text, anchor="w", font=f,
                         fill=TEXT if on else DIM)
        if noted:
            self.create_text(self.w - 10, vy, text=self.disabled_note, anchor="e",
                             font=self.f_note, fill=ACCENT)
        elif on and self._choices:          # a small chevron says "this opens"
            cx, cy = self.w - 15, vy
            self.create_line(cx - 4, cy - 2, cx, cy + 2, cx + 4, cy - 2,
                             fill=TEXT if hot else MUTED, width=1.6,
                             capstyle="round", joinstyle="round")


class StackTile(_Drawn):
    """The focus stack as one tile, so it needs no row of its own.

    A click opens a menu: a tick to turn the stack on (the release then runs it) and
    submenus for frames, step and direction, bound to the caller's variables. The tile
    shows the settings in short form, "3 × XS → ∞", dimmed while the stack is off.
    """

    STEP_SHORT = ("XS", "S", "M", "L", "XL")
    DIR_SHORT = ("\u2192 \u221e", "\u2192 near", "\u2194")

    def __init__(self, parent, on_var, frames_var, step_var, dir_var, frames, steps,
                 dirs, command=None, width=172, height=58, bg=BG):
        super().__init__(parent, width, height, bg=bg)
        self.w, self.h = width, height
        self.on_var, self.frames_var = on_var, frames_var
        self.step_var, self.dir_var = step_var, dir_var
        self.steps, self.dirs = list(steps), list(dirs)
        root = self.winfo_toplevel()
        self.f_label = font(root, 9, "bold")
        self.f_value = font(root, 15)
        self.f_note = font(root, 8, "bold")
        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_checkbutton(label="Focus stack", variable=on_var, command=command)
        self.menu.add_separator()
        for label, var, choices in (("Frames", frames_var, frames),
                                    ("Step", step_var, steps),
                                    ("Direction", dir_var, dirs)):
            sub = tk.Menu(self.menu, tearoff=False)
            for c in choices:
                sub.add_radiobutton(label=str(c), value=c, variable=var)
            self.menu.add_cascade(label=label, menu=sub)
        for var in (on_var, frames_var, step_var, dir_var):
            var.trace_add("write", lambda *_: self.redraw())
        self.redraw()

    def activate(self):
        try:
            self.menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.h + 2)
        finally:
            self.menu.grab_release()

    def _short(self):
        step, d = self.step_var.get(), self.dir_var.get()
        step = self.STEP_SHORT[self.steps.index(step)] if step in self.steps else step
        d = self.DIR_SHORT[self.dirs.index(d)] if d in self.dirs else d
        return "%s \u00d7 %s  %s" % (self.frames_var.get(), step, d)

    def redraw(self):
        self.delete("all")
        on, hot = bool(self.on_var.get()), self._hover
        rounded_rect(self, 1, 1, self.w - 1, self.h - 1, 8,
                     fill=TILE_HOVER if hot else TILE,
                     outline=ACCENT if on else TILE_EDGE, width=1)
        ly, vy = 14, 15 + (self.h - 15) / 2 + 2
        tracked_text(self, 12, ly, "FOCUS STACK", self.f_label, MUTED)
        self.create_text(self.w - 10, ly, text="ON" if on else "OFF", anchor="e",
                         font=self.f_note, fill=ACCENT if on else DIM)
        self.create_text(12, vy, text=self._short(), anchor="w", font=self.f_value,
                         fill=TEXT if on else MUTED if hot else DIM)


class LensTile(_Drawn):
    """The attached lens, live, as a tile beside the exposure tiles.

    Top line: the lens's name ("LENS" when it sends none) and its focal length, with the
    range before it for a zoom. Bottom line: the focus position on a track from near to
    infinity, as a percentage of the lens's travel; the camera reports no distance in
    metres. When there is no position, a short note says why. Read only.
    """

    def __init__(self, parent, width=210, height=58, bg=BG):
        super().__init__(parent, width, height, bg=bg)
        self.configure(cursor="arrow")
        self.w, self.h = width, height
        root = self.winfo_toplevel()
        self.f_label = font(root, 9, "bold")
        self.f_value = font(root, 12)
        self.f_small = font(root, 9)
        self.f_inf = font(root, 13)
        self.value = None              # focus, 0.0 near .. 1.0 infinity
        self.note = None               # why there is no focus value
        self.name = None
        self.focal = None
        self.range = None              # (min, max) mm
        self.redraw()

    def set_focus(self, value, note=None):
        """`value` 0.0 (near) .. 1.0 (infinity), or None with an optional `note` saying
        why there is none, shown in place of the track."""
        if (value, note) != (self.value, self.note):
            self.value, self.note = value, note
            self.redraw()

    def set_lens(self, name, focal, focal_range):
        self.name, self.focal, self.range = name, None, focal_range
        self.set_focal(focal)
        self.redraw()

    def set_focal(self, focal):
        """The current focal length. A zoom can read past the range the camera
        describes (40-85 for one that reaches 90), so the range widens to include it."""
        if focal and self.range:
            lo, hi = self.range
            self.range = (min(lo, focal), max(hi, focal))
        if focal != self.focal:
            self.focal = focal
            self.redraw()

    def clear_lens(self):
        self.name, self.focal, self.range = None, None, None
        self.set_focus(None, "no lens")
        self.redraw()

    def _set_hover(self, on):
        pass

    def activate(self):
        pass

    def redraw(self):
        self.delete("all")
        v = self.value
        on = v is not None
        lens = self.focal is not None or self.name is not None
        rounded_rect(self, 1, 1, self.w - 1, self.h - 1, 8, fill=TILE,
                     outline=TILE_EDGE if (on or lens) else TILE, width=1)

        # top line: name, then the focal length (and range) on the right
        ty = 14
        right = self.w - 12
        focal = "%d mm" % self.focal if self.focal else "--"
        self.create_text(right, ty, text=focal, anchor="e", font=self.f_value,
                         fill=TEXT if self.focal else DIM)
        right -= self.f_value.measure(focal) + 6
        if self.range and self.range[0] != self.range[1]:
            span = "%d\u2013%d" % self.range
            self.create_text(right, ty, text=span, anchor="e", font=self.f_small,
                             fill=MUTED)
            right -= self.f_small.measure(span) + 6
        name = (self.name or "LENS").upper()
        while name and tracked_width(self.f_label, name) > right - 12:
            name = name[:-1]
        tracked_text(self, 12, ty, name, self.f_label, MUTED if lens else DIM)

        # bottom line: the focus track, or why there is none
        by = 40
        if not on:
            self.create_text(12, by, anchor="w", font=self.f_small, fill=MUTED,
                             text=self.note or "focus --")
            return
        pct = "%d%%" % round(v * 100)
        self.create_text(self.w - 12, by, text=pct, anchor="e", font=self.f_value,
                         fill=TEXT)
        x0 = 12 + self.f_small.measure("near") + 7
        x1 = self.w - 12 - self.f_value.measure("100%") - 10 - self.f_inf.measure("\u221e") - 7
        self.create_text(12, by, text="near", anchor="w", font=self.f_small, fill=MUTED)
        self.create_text(x1 + 7, by - 1, text="\u221e", anchor="w", font=self.f_inf,
                         fill=MUTED)
        self.create_line(x0, by, x1, by, fill=SWITCH_OFF, width=3, capstyle="round")
        mx = x0 + (x1 - x0) * v
        self.create_line(x0, by, mx, by, fill=ACCENT, width=3, capstyle="round")
        self.create_oval(mx - 5, by - 5, mx + 5, by + 5, fill="#ffffff", outline="")


class ModeBadge(tk.Canvas):
    """The exposure mode as the camera's dial marks it: a letter, the square for full
    auto, or a small video camera for video. White on a dark tile, no colour."""

    VIDEO = "Video"

    def __init__(self, parent, width=34, height=22, bg=BG):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0)
        self.w, self.h = width, height
        self.f_text = font(self.winfo_toplevel(), 11, "bold")
        self.set("--")

    def set(self, name):
        self.delete("all")
        rounded_rect(self, 0, 0, self.w, self.h, 5, fill=TILE, outline="")
        cx, cy = self.w / 2, self.h / 2
        if name == self.VIDEO:
            # body and lens wedge, as on the dial: the wedge points away from the body
            rounded_rect(self, cx - 9, cy - 5, cx + 3, cy + 5, 2, fill=TEXT, outline="")
            self.create_polygon(cx + 4, cy - 1, cx + 9, cy - 5, cx + 9, cy + 5,
                                cx + 4, cy + 1, fill=TEXT, outline="")
            return
        self.create_text(cx, cy, text=name, font=self.f_text, fill=TEXT)


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
        """The chip is as wide as its pill, never wider, so a chip gridded to the right
        ends exactly where the pill does."""
        self.text = text
        label = self._fit(text, self.max_width - 36)
        self.configure(width=int(self.f_text.measure(label)) + 37)
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
