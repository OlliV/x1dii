"""Tests for the live-view stream parser. No camera needed.

    python3 -m unittest discover tests
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import hbctl, liveview  # noqa: E402


def live_frame(payload, w=640, h=480):
    """A LIVE packet as the camera sends it: the length field covers the whole packet."""
    total = liveview.LIVE_HEADER + len(payload)
    return (b"LIVE" + bytes(8) + total.to_bytes(4, "little")
            + w.to_bytes(4, "little") + h.to_bytes(4, "little") + payload)


def event(body):
    return hbctl.HDR_IN + bytes([len(body)]) + body


class LengthFieldTest(unittest.TestCase):
    """The length field is the total packet length - the bug that halved the frame rate."""

    def test_back_to_back_packets_all_survive(self):
        frames = [live_frame(bytes([i]) * (100 + i)) for i in range(10)]
        got, _ = liveview.frames_from(b"".join(frames))
        self.assertEqual(got, frames)
        _, events, _ = liveview.stream_parse(b"".join(frames))
        self.assertEqual(liveview.stream_parse(b"".join(frames))[0], frames)
        self.assertEqual(events, [])

    def test_payload_has_no_trailing_header_bytes(self):
        f = live_frame(b"\x00\x00\x00\x01\x67sps")
        self.assertEqual(liveview.payload(f), b"\x00\x00\x00\x01\x67sps")

    def test_real_header_from_the_camera(self):
        # first 24 bytes of a 32611-byte packet read from the X1D II
        hdr = (b"LIVE" + (4).to_bytes(4, "little") + (1024).to_bytes(4, "little")
               + (32611).to_bytes(4, "little") + (2756).to_bytes(4, "little")
               + (2064).to_bytes(4, "little"))
        packet = hdr + bytes(32611 - 24)
        got, rest = liveview.frames_from(packet + packet)
        self.assertEqual(got, [packet, packet])
        self.assertEqual(rest, b"")


class StreamParseTest(unittest.TestCase):
    def test_frames_and_events_interleaved(self):
        f, e = live_frame(b"\x00\x00\x01\x67abc"), event(b"\x01\x00\x40\x04")
        frames, events, rest = liveview.stream_parse(b"xx" + f + e + b"yy" + f)
        self.assertEqual(frames, [f, f])
        self.assertEqual(events, [b"\x01\x00\x40\x04"])
        self.assertLessEqual(len(rest), 3)          # only a possible split header is kept

    def test_partial_packet_is_kept(self):
        f = live_frame(b"x" * 100)
        frames, _, rest = liveview.stream_parse(f[:50])
        self.assertEqual(frames, [])
        self.assertEqual(rest, f[:50])

    def test_header_split_across_reads(self):
        e = event(b"hello")
        _, events, rest = liveview.stream_parse(b"junk" + e[:2])
        self.assertEqual(events, [])
        _, events, _ = liveview.stream_parse(rest + e[2:])
        self.assertEqual(events, [b"hello"])

    def test_bad_length_resyncs(self):
        f = live_frame(b"ok")
        bogus = b"LIVE" + bytes(8) + (1 << 30).to_bytes(4, "little") + bytes(8)
        frames, _, _ = liveview.stream_parse(bogus + f)
        self.assertEqual(frames, [f])

    def test_junk_is_linear(self):
        # A stray file transfer on the live-view endpoint used to be parsed one byte at a
        # time with a full copy per byte, pinning a CPU for minutes.
        junk = bytes(range(256)) * (32 << 10)          # 8 MB with no packet headers
        t = time.time()
        frames, events, rest = liveview.stream_parse(junk)
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual((frames, events), ([], []))
        self.assertLessEqual(len(rest), 3)


if __name__ == "__main__":
    unittest.main()
