"""Decoding of exposure-parameter descriptors into menu choices.

The camera describes each settable parameter with a typed string (see hbctl.describe):

  L<t><n>,v1,v2,...     the raw values the parameter accepts
  LS<n>,name1\\tname2    the text the body shows for each value (a separate code)
  LF<n>,hex1,hex2,...   a list of big-endian doubles (ISO; -1 means Auto)

These functions turn that into (order, values): the menu labels in display order and a
map from label to the raw value to write back. Nothing here talks to the camera, so it can
be tested without a body or a UI.
"""
import re
import struct

ISO_AUTO = -1


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
    """Shutter/aperture values are rationals: (numerator << 16) | denominator.

    Accepts the body's own spellings: '1/125', '2:30' (minutes:seconds), '4.8', '30'.
    Returns None for anything else.
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
    except ValueError:
        return None


def _bare(v):
    """A rational as written ('r5000a') and as read back ('5000a') are the same value."""
    if isinstance(v, str) and v[:1] == "r":
        return v[1:]
    return v


def same_value(a, b):
    """Whether a value read back from the camera matches a menu value."""
    a, b = _bare(a), _bare(b)
    if isinstance(a, str) and isinstance(b, str) and a == b:
        return True
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return str(a) == str(b)


def iso_choices(desc):
    """ISO menu from an 'LF<n>,<hex double>,...' descriptor."""
    m = re.match(r"^LF\d+,(.*)$", desc or "")
    order, values = [], {}
    for h in (m.group(1).split(",") if m else []):
        try:
            n = round(struct.unpack(">d", bytes.fromhex(h.rjust(16, "0")))[0])
        except (ValueError, struct.error, OverflowError):
            continue                    # malformed hex, wrong length, or inf/nan
        text = "Auto" if n == ISO_AUTO else str(n)
        values[text] = n
        order.append(text)
    return order, values


def named_choices(desc, names):
    """Aperture/shutter menu from the value descriptor and the name table.

    When the two line up one-to-one the names label the raw values directly. When they do
    not, each name is converted to its rational instead, skipping the T and B modes that
    have no fixed value. With no name table the raw values are shown as they are.
    """
    vals = descriptor_list(desc)
    if names and len(names) == len(vals):
        return list(names), dict(zip(names, vals))
    if names:
        order, values = [], {}
        for n in names:
            if n in ("T", "B"):
                continue
            v = rational(n)
            if v is not None:
                order.append(n)
                values[n] = "r%x" % v
        return order, values
    return list(vals), {v: v for v in vals}


def _thirds(k):
    """-4 -> '−1⅓': an EV step count in thirds, spelled as cameras print it."""
    if k == 0:
        return "0"
    whole, third = divmod(abs(k), 3)
    text = ("%d" % whole if whole else "") + ("", "\u2153", "\u2154")[third]
    return ("+" if k > 0 else "\u2212") + text


def ev_choices(desc):
    """EV menu from a range descriptor 'MLf<min>,<max>,<step>' (big-endian doubles).

    The X1D II reports -5..+5 in thirds. The value written back is k * step with the
    camera's own step (a single-precision third), so it reads back exactly as written.
    """
    m = re.match(r"^M?Lf([0-9a-f]+),([0-9a-f]+),([0-9a-f]+)$", desc or "")
    if not m:
        return [], {}
    try:
        lo, hi, step = (struct.unpack(">d", bytes.fromhex(h.rjust(16, "0")))[0]
                        for h in m.groups())
    except (ValueError, struct.error):
        return [], {}
    if not step > 0 or not lo <= hi:
        return [], {}
    order, values = [], {}
    for k in range(round(lo / step), round(hi / step) + 1):
        text = _thirds(k) if abs(step * 3 - 1) < 1e-6 else "%+.1f" % (k * step)
        order.append(text)
        values[text] = k * step
    return order, values


def rational_text(raw):
    """A raw rational read from the body ('5000a' = 5/10) in the body's own spelling.

    Used when the camera reports a value the menu does not list - the metered shutter
    speed in A mode, say - so the tile can still show what the camera is doing.
    Returns None if `raw` is not a rational.
    """
    try:
        v = int(_bare(str(raw)), 16)
    except ValueError:
        return None
    num, den = v >> 16, v & 0xffff
    if not num or not den:
        return None
    x = num / den
    if x < 0.3:                                       # fractions: 1/125
        return "1/%d" % round(den / num)
    if x >= 60:                                       # minutes: 2:30
        return "%d:%02d" % (int(x) // 60, round(x) % 60)
    return ("%.1f" % x).rstrip("0").rstrip(".")       # seconds or f-number: 0.5, 4, 5.6


def current_choice(order, values, current):
    """The menu label matching what the camera reports, else the first label, else None."""
    for label, raw in values.items():
        if same_value(raw, current):
            return label
    return order[0] if order else None
