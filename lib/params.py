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


def same_value(a, b):
    """Whether a value read back from the camera matches a menu value."""
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


def current_choice(order, values, current):
    """The menu label matching what the camera reports, else the first label, else None."""
    for label, raw in values.items():
        if same_value(raw, current):
            return label
    return order[0] if order else None
