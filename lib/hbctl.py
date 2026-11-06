#!/usr/bin/env python3
"""hbctl - host driver for the Hasselblad X1D II over USB vendor interface 3.

Protocol recovered by tracing Phocus 4.1.1's libusb traffic; see PROTOCOL.md.

  host -> camera   ep 0x04     04 00 08 05 <n> <payload n>
  camera -> host   ep 0x85     03 00 05 08 <n> <payload n>
  camera -> host   ep 0x84     bulk data (live view, storage blocks)

  payload       = <seq u16 LE> <body>
  request body  = <op u8> <code u16 LE> [value]
  reply body    = <0x80|op u8> <data>          (0x40 in that slot = async event)

Opcodes
  1  command / trigger     5  list parameter codes
  2  read value            6  describe parameter
  3  write value           7  data block (storage)

Values are typed ASCII in both directions:
  i<n>            integer             I<count>,<v1>,..   integer list / enum
  f<16 hex>       double, big-endian  F<count>,<hex..>   double list
  S<len>,<text>   string              r<a,b,c>          packed rational

Writes go out as op 3 using the same `I<n>,<values>` form the camera reads back.

Commands
  hbctl.py list                       enumerate advertised parameters
  hbctl.py read ExposureTime Iso      read one or more
  hbctl.py write Iso 400              write, then verify by read-back
  hbctl.py describe LiveVideoMode     descriptor (range / enum / table)
  hbctl.py dump                       everything: value + descriptor
  hbctl.py watch Iso Battery          poll and print changes
  hbctl.py snapshot out.json          full state as JSON
  hbctl.py session                    establish the tether-transfer session
  hbctl.py download [dir] [--session] capture and pull the raw off the camera
  hbctl.py capture [count] [delay]    fire the shutter
  hbctl.py focus-stack [frames] [step] [init_s] [exp_ms] [dir]
                                      run the camera's focus bracketing
  hbctl.py storage                    list the card
  hbctl.py raw <op> <code> [hex]      arbitrary frame (writes need --write)

Nothing here touches persistent state: every command is one the camera's own
software issues, and `write` always reports the read-back, so a rejected value is
visible rather than silent.
"""
import json
import os
import re
import struct
import sys
import threading
import time

try:
    import usb.core
    import usb.util
except ImportError:
    # developer convenience only: pyusb installed outside site-packages. The app bundle
    # vendors it, so this is never reached in the packaged build.
    _dev = "/tmp/pylibs"
    if os.path.isdir(_dev):
        sys.path.insert(0, _dev)
    import usb.core
    import usb.util

VID, PID = 0x2756, 0x0007
IFACE = 3
EP_OUT, EP_IN, EP_DATA = 0x04, 0x85, 0x84

OP_CMD, OP_READ, OP_WRITE, OP_INDEXED, OP_LIST, OP_DESCRIBE, OP_BLOCK = 1, 2, 3, 4, 5, 6, 7

HDR_OUT = bytes([0x04, 0x00, 0x08, 0x05])   # host -> camera control frame
HDR_IN = bytes([0x03, 0x00, 0x05, 0x08])    # camera -> host control frame

CODE_RELEASE = 0x0001                       # op 1: fire the shutter
CODE_LIVE_VIEW = 0x0007                     # op 1: start/stop the preview stream
LIVE_START = bytes([0x0C]) + bytes(9)
LIVE_STOP = bytes(10)

EVT_CAPTURE = 0x40                          # capture offered to the host (tether token)
EVT_FILE = 0x45                             # file written to the card

NAMES = {}


def _load_table():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parameter_table.txt")
    try:
        with open(path) as fh:
            for line in fh:
                m = re.match(r"\s*(0x[0-9a-fA-F]{4})\s+(\S+)", line)
                if m:
                    NAMES[int(m.group(1), 16)] = m.group(2)
    except OSError:
        pass


_load_table()


def name_of(code):
    return NAMES.get(code, "")


def resolve(text):
    t = str(text).lower()
    if t.startswith("0x"):
        return int(t, 16)
    for k, v in NAMES.items():
        if v.lower() == t:
            return k
    try:
        return int(t, 0)
    except ValueError:
        raise SystemExit("unknown parameter %r (try `list`)" % text)


# ---------------------------------------------------------------- values

def decode_typed(d):
    """Decode a module-2 reply into (tag, payload)."""
    if not d:
        return None, None
    tag = chr(d[0])
    rest = d[1:]
    try:
        if tag == "i":
            return "i", int(rest.decode())
        if tag == "f":
            return "f", struct.unpack(">d", bytes.fromhex(rest.decode().rjust(16, "0")))[0]
        if tag == "S":
            _, _, s = rest.decode("latin-1").partition(",")
            return "S", s
        if tag == "I":
            parts = rest.decode("latin-1").split(",")
            return "I", [int(x) for x in parts[1:] if x != ""]
        if tag == "r":
            return "r", rest.decode("latin-1")
    except Exception:
        pass
    return tag, rest.decode("latin-1", "replace")


def encode_typed(tag, value):
    """Encode a Python value into the camera's typed-string form."""
    s = str(value)
    if len(s) > 1 and s[0] in "ISfr" and "," in s:
        return s.encode()                      # caller supplied the typed form
    if tag == "i":
        return ("I1,%d" % int(value)).encode()
    if tag == "f":
        return ("f" + struct.pack(">d", float(value)).hex()).encode()
    if tag == "I":
        vals = value if isinstance(value, (list, tuple)) else [value]
        return ("I%d,%s" % (len(vals), ",".join(str(int(v)) for v in vals))).encode()
    if tag == "r":
        return ("r%s" % s).encode()
    if tag == "S":
        return ("S%d,%s" % (len(s), s)).encode()
    return s.encode()


# ---------------------------------------------------------------- transport

def _find_tiff(buf):
    """Index of the TIFF header in a byte buffer, preferring the earliest."""
    a = buf.find(b"II*\x00")
    b = buf.find(b"MM\x00*")
    hits = [i for i in (a, b) if i >= 0]
    return min(hits) if hits else -1


def tiff_extent(data):
    """Total byte length a Hasselblad 3FR declares for itself, or None.

    A 3FR is a TIFF whose IFD0 is a preview and whose tag 330 points at SubIFDs
    holding the raw. The largest strip offset+length across all of them is the file
    size; reading past it just appends whatever the camera sends next.
    """
    if len(data) < 16 or data[:4] not in (b"II*\x00", b"MM\x00*"):
        return None
    bo = "<" if data[:2] == b"II" else ">"
    sz = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}

    def ifd(o):
        cnt = struct.unpack_from(bo + "H", data, o)[0]
        ents = {}
        for i in range(cnt):
            q = o + 2 + 12 * i
            tag, typ, c = struct.unpack_from(bo + "HHI", data, q)
            total = sz.get(typ, 1) * c
            if total <= 4:
                v = (struct.unpack_from(bo + "H", data, q + 8)[0] if typ == 3
                     else struct.unpack_from(bo + "I", data, q + 8)[0])
            else:
                v = (struct.unpack_from(bo + "I", data, q + 8)[0], total)
            ents[tag] = (typ, c, v)
        nxt = struct.unpack_from(bo + "I", data, o + 2 + 12 * cnt)[0]
        return ents, nxt

    def arr(ent):
        typ, c, v = ent
        if isinstance(v, tuple):
            fmt = "I" if typ in (4, 9) else "H"
            return list(struct.unpack_from(bo + "%d%s" % (c, fmt), data, v[0]))
        return [v]

    try:
        first = struct.unpack_from(bo + "I", data, 4)[0]
        e0, _ = ifd(first)
        offsets = [first]
        if 330 in e0:
            offsets += arr(e0[330])
        end = 0
        for o in offsets:
            ents, _ = ifd(o)
            if 273 in ents and 279 in ents:
                for start, count in zip(arr(ents[273]), arr(ents[279])):
                    end = max(end, start + count)
        return end or None
    except Exception:
        return None


def _backend():
    """Use a bundled libusb when the environment names one.

    A .app bundle cannot rely on Homebrew being installed or on /opt/homebrew/lib being
    in the loader's search path, so the launcher sets HBLIBUSB to the dylib it ships.
    """
    path = os.environ.get("HBLIBUSB")
    if not path or not os.path.exists(path):
        return None
    try:
        import usb.backend.libusb1 as libusb1
        return libusb1.get_backend(find_library=lambda _name: path)
    except Exception:
        return None


class Camera:
    def __init__(self, timeout=1500):
        self.timeout = timeout
        self.seq = int(time.time() * 1000) & 0x3FFF
        self.on_event = None              # called with async (0x40) payloads
        self.io_lock = threading.RLock()  # replies and events share endpoint 0x85
        self.session_started = False
        self.pkt_stats = {}               # opcode -> count, for diagnosing the channel
        self.last_capture_path = None     # card path from the latest 0x45 event
        self.last_download_expected = None
        self.last_session_sent = 0
        self.dev = usb.core.find(idVendor=VID, idProduct=PID, backend=_backend())
        if self.dev is None:
            raise SystemExit("camera not found (VID %04x PID %04x)" % (VID, PID))
        self.cfg = self.dev.get_active_configuration()
        self.intf = self.cfg[(IFACE, 0)]
        usb.util.claim_interface(self.dev, IFACE)

    def close(self):
        if getattr(self, "session_started", False):
            self.end_session()
            self.session_started = False
        try:
            usb.util.release_interface(self.dev, IFACE)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass
        self.dev = None          # drop the reference while the backend is still alive

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _frame(self, op, code, value=b""):
        """Build a request frame under the next sequence number. Returns (seq, frame)."""
        self.seq = (self.seq + 1) & 0x7FFF
        payload = struct.pack("<HBH", self.seq, op, code) + value
        return self.seq, HDR_OUT + bytes([len(payload)]) + payload

    def _send(self, op, code, value=b""):
        """Write one request frame without waiting for a reply. Returns its seq."""
        seq, frame = self._frame(op, code, value)
        self.dev.write(EP_OUT, frame, timeout=self.timeout)
        return seq

    def _read_ctrl(self, timeout):
        """One control packet's payload from 0x85, or None on timeout or a non-control read."""
        try:
            b = bytes(self.dev.read(EP_IN, 1024, timeout=timeout))
        except usb.core.USBTimeoutError:
            return None
        if len(b) < 7 or b[0] != HDR_IN[0]:
            return None
        return b[5:5 + b[4]]

    def _recv(self, want_seq=None, deadline=2.5):
        end = time.time() + deadline
        while time.time() < end:
            try:
                with self.io_lock:
                    data = self.dev.read(EP_IN, 1024, timeout=self.timeout)
            except usb.core.USBTimeoutError:
                return None
            b = bytes(data)
            if len(b) < 7 or b[0] != 0x03 or b[1] != 0x00:
                continue
            payload = b[5:5 + b[4]]
            if len(payload) < 3:
                continue
            seq = struct.unpack("<H", payload[:2])[0]
            status = payload[2]
            self.pkt_stats[status] = self.pkt_stats.get(status, 0) + 1
            if 0x40 <= status <= 0x4f:
                # an async event, not an answer to this call - hand it on rather than
                # dropping it, or a capture made with the camera's own shutter is lost
                if self.on_event:
                    try:
                        self.on_event(payload)
                    except Exception:
                        pass
                continue
            if want_seq is not None and seq != want_seq:
                continue
            return status, payload[3:]
        return None

    def call(self, op, code, value=b"", retries=1):
        """Send a request and wait for its reply. Returns (status, data) or None.

        A retry resends the identical frame, sequence number included.
        """
        seq, frame = self._frame(op, code, value)
        for _ in range(retries + 1):
            self.dev.write(EP_OUT, frame, timeout=self.timeout)
            r = self._recv(want_seq=seq)
            if r is not None:
                return r
        return None

    # ---- high level

    def list_codes(self):
        r = self.call(OP_LIST, 0)
        if not r:
            return []
        data = r[1]
        return [struct.unpack("<H", data[i:i + 2])[0] for i in range(0, len(data) - 1, 2)]

    def read_raw(self, code):
        return self.call(OP_READ, code)

    def read(self, code):
        r = self.read_raw(code)
        if not r or not r[1]:
            return None
        return decode_typed(r[1])[1]

    def describe(self, code):
        r = self.call(OP_DESCRIBE, code)
        if not r or len(r[1]) < 3:
            return None
        return r[1][2:].decode("latin-1", "replace")

    def write(self, code, value):
        """Write and verify. Returns (applied, before, after, ack)."""
        raw = self.read_raw(code)
        tag, before = decode_typed(raw[1]) if raw else (None, None)
        payload = encode_typed(tag, value)
        r = self.call(OP_WRITE, code, payload)
        ack = r[1].hex(" ") if r else "no reply"
        time.sleep(0.05)
        after = self.read(code)
        try:
            applied = abs(float(after) - float(value)) < 1e-9
        except (TypeError, ValueError):
            applied = str(after) == str(value)
        return applied, before, after, ack

    def release(self, timeout=15.0, commit=True):
        """Fire the shutter, then complete the capture handshake.

        1. op 1 / code 0x0001 / empty      -> shutter actuates
        2. camera sends event 0x40 carrying `04 <token u32>`
        3. op 7 / 0x0004 with the token    -> ack 0
        4. op 1 / 0x0009 with the token

        Steps 3-4 are what Phocus always sends after a capture and appear to be the
        commit handshake; without them the body fires but no file is written.
        Returns (token, [ack3, ack4]).
        """
        self.last_capture_path = None
        self._send(OP_CMD, CODE_RELEASE)

        token = None
        end = time.time() + timeout
        while time.time() < end:
            p = self._read_ctrl(self.timeout)
            if p is None:
                continue
            if len(p) >= 3 and p[2] == EVT_CAPTURE:
                # older/firmware-varies: capture event carrying a token
                data = p[3:]
                token = int.from_bytes(data[1:5], "little") if len(data) >= 5 else None
                break
            if len(p) >= 3 and p[2] == EVT_FILE:
                # this firmware announces the written file instead:
                #   <01 00> <id u32> <path>
                data = p[3:]
                if len(data) >= 6:
                    token = int.from_bytes(data[2:6], "little")
                    self.last_capture_path = data[6:].decode("latin-1", "replace")
                break
        if token is None or not commit or self.last_capture_path:
            # when the body wrote the file itself and told us the path, there is
            # nothing to commit - Phocus's extra two frames are for tether transfer
            return token, []

        v = bytes([0, 0]) + struct.pack("<H", token & 0xFFFF) + bytes(10)
        a1 = self.call(OP_BLOCK, 0x0004, v)
        a2 = self.call(OP_CMD, 0x0009, struct.pack("<I", token))
        return token, [a1[1].hex(" ") if a1 else "no reply",
                       a2[1].hex(" ") if a2 else "no reply"]

    def flush_stream(self, seconds=3.0):
        """Drain live view for a while. Keeps the camera awake (it refuses transfers
        while the screen is off) and clears any queued frames."""
        end = time.time() + seconds
        n = 0
        while time.time() < end:
            try:
                self.dev.read(EP_DATA, 1 << 18, timeout=200)
                n += 1
            except Exception:
                pass
        return n

    def focus_bracket(self, frames=3, step=0, initial_delay=None,
                      exposure_delay=None, direction=0, timeout=60.0, idle=4.0,
                      progress=None):
        """Run the camera's own focus bracketing and return the files it wrote.

        Live view must be OFF: with the preview streaming, a host trigger takes a single
        frame and cancels the bracket, so the sequence never runs. Focus bracketing also
        needs the lens to have travel left in both directions - "Lens limit reached" means
        it ran out, so start from a mid-range focus position or use fewer frames.

        `step` is the camera's named scale, 0..4 = ExtraSmall, Small, Medium, Large,
        ExtraLarge (confirmed by changing the menu and watching 0x006a). It is NOT a
        distance: 0x006d, which looks like one (125..16000), does not follow it.

        `direction` is 0x006e, confirmed the same way: 0 = towards infinity,
        1 = towards near, 2 = symmetric (near and far).
        """
        self.stop_live()
        time.sleep(0.6)
        self.write(0x006b, frames)                          # FocusBracketingFrames
        self.write(0x006a, step)                            # step size: 0..4 named scale
        if initial_delay is not None:                       # 0x006c, seconds
            self.write(0x006c, initial_delay)
        if exposure_delay is not None:                      # 0x006d, milliseconds
            self.write(0x006d, exposure_delay)
        if direction is not None:                           # 0x006e, 0 inf / 1 near / 2 both
            self.write(0x006e, direction)
        self.write(0x0041, 5)                               # DriveModeMode = focus bracket
        time.sleep(0.15)
        if self.read(0x0041) != 5:
            self.write(0x0041, 5)                           # the first write can be dropped
            time.sleep(0.15)
            if self.read(0x0041) != 5:
                raise RuntimeError("camera refused drive mode 5")

        self._send(OP_CMD, CODE_RELEASE)

        seen, end = [], time.time() + timeout
        started = last_new = time.time()
        while time.time() < end and len(seen) < frames:
            if not seen and time.time() - started > 8:
                break                     # no first frame: camera never started the run
            if seen and time.time() - last_new > idle:
                break                     # stopped early (lens limit, or busy)
            p = self._read_ctrl(800)
            if p is None:
                continue
            if len(p) >= 3 and p[2] == EVT_FILE:
                path = p[3:][6:].decode("latin-1", "replace")
                if path.startswith("/SD1"):        # one event per slot; count the card once
                    seen.append(path)
                    last_new = time.time()
                    if progress:
                        try:
                            progress(path, len(seen))
                        except Exception:
                            pass
                    if len(seen) >= frames:
                        break
        self.write(0x0041, 0)                              # back to single shot
        return seen

    def capture_token(self, timeout=20.0):
        """Trigger a capture and wait specifically for the 0x40 tether token.

        Unlike release(), this ignores the 0x45 "file written" event: when the camera
        is in a tether-transfer session it offers the image to the host and that token is
        what `download` needs. Falls back to None if no token is offered.
        """
        self._send(OP_CMD, CODE_RELEASE)
        end = time.time() + timeout
        while time.time() < end:
            p = self._read_ctrl(self.timeout)
            if p is None:
                continue
            if len(p) >= 3 and p[2] == EVT_CAPTURE:
                return int.from_bytes(p[4:8], "little")
        return None

    def download(self, token, outfile, max_bytes=1 << 28, idle=6.0):
        """Pull the pending capture for `token` off the camera. Returns bytes written.

        The camera streams the file on 0x84 - the same endpoint as the live view - so
        nothing else may be reading it while this runs, and the stream may still begin
        with a stray preview frame. We therefore resync on the TIFF header, then stop at
        the length the TIFF declares for itself: the camera keeps sending past the end
        of the file, which appends junk that other raw readers choke on.
        """
        self.last_download_expected = None
        arg = bytes([0, 0]) + token.to_bytes(2, "little") + bytes(10)
        ok = False
        for _ in range(3):
            r = self.call(OP_BLOCK, 0x0004, arg)
            if r and r[1] == b"\x00\x00\x00\x00":
                ok = True
                break
            time.sleep(0.6)
        if not ok:
            return 0

        total = 0
        expected = None
        started = False
        prefix = bytearray()
        beg = bytearray()
        last = time.time()
        with open(outfile, "wb") as fh:
            while time.time() - last < idle and total < max_bytes:
                try:
                    chunk = bytes(self.dev.read(EP_DATA, 8 << 20, timeout=1500))
                except Exception:
                    break
                if not chunk:
                    continue
                last = time.time()

                if not started:
                    prefix += chunk
                    idx = _find_tiff(prefix)
                    if idx < 0:
                        if len(prefix) > (1 << 22):
                            break                   # never became a TIFF
                        continue
                    prefix = prefix[idx:]
                    fh.write(prefix)
                    total = len(prefix)
                    beg += prefix
                    started = True
                else:
                    fh.write(chunk)
                    total += len(chunk)
                    if len(beg) < 65536:
                        beg += chunk

                if expected is None and len(beg) >= 65536:
                    expected = tiff_extent(bytes(beg[:65536]))
                    self.last_download_expected = expected
                if expected and total >= expected:
                    fh.truncate(expected)
                    total = expected
                    break
        return total

    def end_session(self):
        """Release a tether session the way Phocus does.

        Phocus sends these two commands on disconnect. Without them the camera is left
        believing a host is still attached, and the next connection tends to fail with an
        endpoint overflow that only a power cycle clears.
        """
        try:
            # raw typed payload, exactly as Phocus sends it: "I2,0,1"
            self.call(OP_WRITE, 0x007a, b"I2,0,1", retries=0)
            self.call(OP_CMD, 0x0004, b"\x00\x00\x01", retries=0)
        except Exception:
            pass

    def replay_session(self, verbose=False, live=True):
        """Replay Phocus's connect sequence.

        This is what makes captures offer themselves to the host (a 0x40 token event)
        instead of only being written to the card. Once per connection is enough -
        subsequent captures keep offering tokens.

        `live=False` omits the single command that starts live view. That matters: while
        the preview streams, the camera refuses to fire from its own shutter button or
        cable release, and it does not recover when the stream is merely stopped - live
        view has to stay off. A working cable-release Phocus session contains no
        op 1 / 0x0007 at all, which is the same rule seen from the other side.

        Returns the commands that got no reply, so a caller can tell a real session from
        a silent one.
        """
        from . import liveview as lv
        seq = lv.load_sequence()
        failed = []
        sent = 0
        for op, code, val in seq:
            if not live and op == OP_CMD and code == CODE_LIVE_VIEW:
                continue                    # leave live view off: the shutter needs it
            sent += 1
            try:
                if self.call(op, code, val, retries=0) is None:
                    failed.append((op, code))
            except Exception:
                failed.append((op, code))
            if op == OP_BLOCK:
                lv.drain(self, 200)
        if failed:
            print("session: %d of %d commands got no reply: %s"
                  % (len(failed), sent, failed[:8]))
        if verbose:
            print("session replayed (%d of %d commands, live=%s)"
                  % (sent - len(failed), sent, live))
        self.session_started = True
        self.last_session_sent = sent
        return failed

    def start_live(self):
        """Start the preview stream: op 1 / 0x0007 with first byte 0x0c."""
        return self.call(OP_CMD, CODE_LIVE_VIEW, LIVE_START)

    def stop_live(self):
        """Stop the preview stream.

        Note that stopping is not the same as never having started it: the body shutter
        is released again as soon as the stream stops, but a capture will only be offered
        to the host while the tether session is still active.
        """
        return self.call(OP_CMD, CODE_LIVE_VIEW, LIVE_STOP, retries=0)

    def storage(self, expected=2770004):
        """List the card. The blob arrives on 0x84, possibly in several reads."""
        r = self.call(OP_BLOCK, 0x0009, bytes.fromhex("0000000000000000000054442a00"))
        if not r or r[1] != b"\x00\x00\x00\x00":
            return None
        blob = b""
        while len(blob) < expected:
            try:
                chunk = bytes(self.dev.read(EP_DATA, min(expected - len(blob), 1 << 20),
                                            timeout=1500))
            except usb.core.USBTimeoutError:
                break
            if not chunk:
                break
            blob += chunk
        if len(blob) < 26:
            return None                     # not a listing blob
        out, i = [], 25
        while i < len(blob) - 1:
            n = blob[i]
            if 3 <= n <= 120 and blob[i + 1:i + 2] == b"/":
                s2 = blob[i + 1:i + 1 + n]
                if all(32 <= c < 127 for c in s2):
                    out.append(s2.decode())
                    i += 1 + n
                    continue
            i += 1
        return out


# ---------------------------------------------------------------- cli

def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return
    allow_write = "--write" in argv
    argv = [a for a in argv if a != "--write"]
    cmd, rest = argv[0], [a for a in argv[1:] if not a.startswith("--")]

    with Camera() as cam:
        if cmd == "list":
            codes = cam.list_codes()
            print("%d advertised parameters" % len(codes))
            for c in codes:
                print("  0x%04x %s" % (c, name_of(c)))

        elif cmd == "read":
            for a in rest:
                c = resolve(a)
                print("0x%04x %-22s %r" % (c, name_of(c), cam.read(c)))

        elif cmd == "write":
            if len(rest) < 2:
                raise SystemExit("usage: write <parameter> <value>")
            c = resolve(rest[0])
            value = " ".join(rest[1:])
            for conv in (int, float):
                try:
                    value = conv(value)
                    break
                except ValueError:
                    pass
            applied, before, after, ack = cam.write(c, value)
            print("%-22s %r -> %r   (was %r, ack %s)%s" % (
                name_of(c) or hex(c), value, after, before, ack,
                "" if applied else "   NOT APPLIED"))

        elif cmd == "describe":
            for a in rest:
                c = resolve(a)
                print("0x%04x %-22s %s" % (c, name_of(c), cam.describe(c) or "-"))

        elif cmd == "dump":
            codes = cam.list_codes()
            print("%d parameters\n" % len(codes))
            for c in codes:
                print("0x%04x %-24s = %-30r %s" % (
                    c, name_of(c), cam.read(c), cam.describe(c) or ""))

        elif cmd == "watch":
            codes = [resolve(a) for a in rest] or [0x002f, 0x0008, 0x0009, 0x000a]
            last = {}
            print("watching %s  (ctrl-c to stop)"
                  % ", ".join(name_of(c) or hex(c) for c in codes))
            try:
                while True:
                    for c in codes:
                        v = cam.read(c)
                        if c in last and last[c] != v:
                            print("%s  %-20s %r -> %r" % (
                                time.strftime("%H:%M:%S"), name_of(c) or hex(c), last[c], v))
                        last[c] = v
                    time.sleep(0.4)
            except KeyboardInterrupt:
                print()

        elif cmd == "snapshot":
            out = rest[0] if rest else "snapshot.json"
            state = {}
            for c in cam.list_codes():
                key = "0x%04x %s" % (c, name_of(c)) if name_of(c) else "0x%04x" % c
                state[key] = {
                    "code": "0x%04x" % c,
                    "value": cam.read(c),
                    "descriptor": cam.describe(c),
                }
            with open(out, "w") as fh:
                json.dump(state, fh, indent=1, default=str)
            print("wrote %s (%d parameters)" % (out, len(state)))

        elif cmd == "capture":
            n = int(rest[0]) if rest else 1
            for i in range(n):
                token, acks = cam.release()
                if token is None:
                    print("shot %d: no capture event" % (i + 1))
                    break
                print("shot %d: token 0x%04x  commit acks %s" % (i + 1, token, acks))
                if i + 1 < n:
                    time.sleep(float(rest[1]) if len(rest) > 1 else 2.0)

        elif cmd == "focus-stack":
            frames = int(rest[0]) if rest else 3
            step = int(rest[1]) if len(rest) > 1 else 0
            init_delay = int(rest[2]) if len(rest) > 2 else None      # seconds
            exp_delay = int(rest[3]) if len(rest) > 3 else None       # milliseconds
            direction = int(rest[4]) if len(rest) > 4 else 0          # 0 inf, 1 near, 2 both
            names = {0: "ExtraSmall", 1: "Small", 2: "Medium", 3: "Large", 4: "ExtraLarge"}
            print("focus bracketing: %d frames, step %s (live view must be off)"
                  % (frames, names.get(step, step)))
            files = cam.focus_bracket(frames=frames, step=step,
                                      initial_delay=init_delay,
                                      exposure_delay=exp_delay,
                                      direction=direction)
            print("captured %d/%d frames:" % (len(files), frames))
            for f in files:
                print("   ", f)

        elif cmd == "session":
            cam.replay_session(verbose=True)

        elif cmd == "download":
            outdir = rest[0] if rest else "."
            if "--session" in sys.argv:
                cam.replay_session(verbose=True)
            # the body refuses transfers while its screen is off; live view streaming
            # is what keeps it awake, so pump a few seconds of it before triggering
            cam.flush_stream(3.0)
            time.sleep(0.5)
            token = cam.capture_token()
            if token is None:
                print("no tether token: the session is not established "
                      "(try --session, and keep live view running)")
                return
            name = (cam.last_capture_path or "").rsplit("/", 1)[-1]
            if not name:
                name = "capture_%04x.3fr" % token
            out = os.path.join(outdir, name)
            n = cam.download(token, out)
            if n:
                print("downloaded %s (%d bytes, %.1f MiB)" % (out, n, n / 1048576.0))
            else:
                print("transfer refused (ack) - the session is probably not established")

        elif cmd == "storage":
            paths = cam.storage()
            if not paths:
                print("no listing returned")
            else:
                print("%d entries" % len(paths))
                for p in paths:
                    print("   ", p)

        elif cmd == "raw":
            if len(rest) < 2:
                raise SystemExit("usage: raw <op> <code> [hexbytes]")
            op = int(rest[0], 0)
            c = resolve(rest[1])
            val = bytes.fromhex(rest[2]) if len(rest) > 2 else b""
            if not allow_write and op in (OP_CMD, OP_WRITE, OP_INDEXED, OP_BLOCK):
                raise SystemExit("refusing op %d without --write" % op)
            print(cam.call(op, c, val))

        else:
            print(__doc__)
