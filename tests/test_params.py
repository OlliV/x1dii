"""Tests for lib/params.py and the status line. No camera or display needed.

    python3 -m unittest discover tests
"""
import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib import params  # noqa: E402


def lf(*values):
    """Build an ISO-style 'LF<n>,<hex double>,...' descriptor."""
    hexes = [struct.pack(">d", v).hex() for v in values]
    return "LF%d,%s" % (len(hexes), ",".join(hexes))


class DescriptorTest(unittest.TestCase):
    def test_descriptor_list(self):
        self.assertEqual(params.descriptor_list("LR3,a,b,c"), ["a", "b", "c"])
        self.assertEqual(params.descriptor_list(""), [])
        self.assertEqual(params.descriptor_list(None), [])
        self.assertEqual(params.descriptor_list("i5"), [])

    def test_name_table(self):
        self.assertEqual(params.name_table("LS7,4\t4.8"), ["4", "4.8"])
        self.assertEqual(params.name_table(None), [])
        self.assertEqual(params.name_table("LR3,a"), [])


class RationalTest(unittest.TestCase):
    def test_spellings(self):
        self.assertEqual(params.rational("1/125"), (1 << 16) | 125)
        self.assertEqual(params.rational("30"), (30 << 16) | 1)
        self.assertEqual(params.rational("4.8"), (48 << 16) | 10)
        self.assertEqual(params.rational("2:30"), (230 << 16) | 100)

    def test_rejects_non_numbers(self):
        for bad in ("T", "B", "", "1/2/3", "x.y"):
            self.assertIsNone(params.rational(bad), bad)


class ChoicesTest(unittest.TestCase):
    def test_iso(self):
        order, values = params.iso_choices(lf(-1, 100, 200))
        self.assertEqual(order, ["Auto", "100", "200"])
        self.assertEqual(values, {"Auto": -1, "100": 100, "200": 200})

    def test_iso_skips_malformed(self):
        order, _ = params.iso_choices("LF3,zz,%s,%s" % (
            struct.pack(">d", 400).hex(), "1" * 20))
        self.assertEqual(order, ["400"])
        self.assertEqual(params.iso_choices(None), ([], {}))

    def test_names_match_values(self):
        order, values = params.named_choices("LR2,r1,r2", ["4", "4.8"])
        self.assertEqual(order, ["4", "4.8"])
        self.assertEqual(values, {"4": "r1", "4.8": "r2"})

    def test_names_converted_when_counts_differ(self):
        order, values = params.named_choices("LR1,x", ["B", "1/125", "T", "30"])
        self.assertEqual(order, ["1/125", "30"])
        self.assertEqual(values["1/125"], "r%x" % ((1 << 16) | 125))

    def test_no_names(self):
        self.assertEqual(params.named_choices("LR2,a,b", []),
                         (["a", "b"], {"a": "a", "b": "b"}))

    def test_current_choice(self):
        order, values = ["Auto", "100", "200"], {"Auto": -1, "100": 100, "200": 200}
        self.assertEqual(params.current_choice(order, values, 200.0), "200")
        self.assertEqual(params.current_choice(order, values, 999), "Auto")
        self.assertIsNone(params.current_choice([], {}, 1))

    def test_same_value(self):
        self.assertTrue(params.same_value("r1", "r1"))
        self.assertTrue(params.same_value(100, "100.0000000001"))
        self.assertFalse(params.same_value(None, 0))


class ReadBackTest(unittest.TestCase):
    """Values read from the body against the menu built from its descriptors."""

    def test_written_and_read_rationals_match(self):
        # the menu writes 'r5000a'; the camera reads back '5000a'
        self.assertTrue(params.same_value("r5000a", "5000a"))
        self.assertTrue(params.same_value("5000a", "r5000a"))
        self.assertFalse(params.same_value("r5000a", "50014"))

    def test_shutter_found_in_rational_menu(self):
        order, values = params.named_choices("LR2,x,y", ["1/125", "0.5", "B"])
        self.assertEqual(params.current_choice(order, values, "5000a"), "0.5")

    def test_rational_text(self):
        self.assertEqual(params.rational_text("5000a"), "0.5")
        self.assertEqual(params.rational_text("1007d"), "1/125")
        self.assertEqual(params.rational_text("40001"), "4")
        self.assertEqual(params.rational_text("38000a"), "5.6")
        self.assertEqual(params.rational_text("%x" % ((150 << 16) | 1)), "2:30")
        self.assertIsNone(params.rational_text("zz"))
        self.assertIsNone(params.rational_text("0"))


class EvTest(unittest.TestCase):
    # as the X1D II describes EvAdjust: -5..+5 in single-precision thirds
    DESC = "MLfc014000000000000,4014000000000000,3fd5555560000000"

    def test_thirds(self):
        order, values = params.ev_choices(self.DESC)
        self.assertEqual(len(order), 31)
        self.assertEqual(order[0], "\u22125")
        self.assertEqual(order[-1], "+5")
        self.assertIn("0", order)
        self.assertIn("+1\u2153", order)
        self.assertIn("\u2212\u2154", order)
        self.assertEqual(values["0"], 0.0)
        self.assertAlmostEqual(values["+1"], 1.0, places=6)

    def test_read_back_matches(self):
        order, values = params.ev_choices(self.DESC)
        self.assertEqual(params.current_choice(order, values, 0.6666666865348816),
                         "+\u2154")

    def test_bad_descriptor(self):
        self.assertEqual(params.ev_choices("LI2,0,1"), ([], {}))
        self.assertEqual(params.ev_choices(None), ([], {}))


class StatusTextTest(unittest.TestCase):
    """capture/viewer.status_text: pure, but it lives beside the UI it describes."""

    @classmethod
    def setUpClass(cls):
        try:
            sys.path.insert(0, os.path.join(ROOT, "capture"))
            import viewer
        except ImportError as e:            # tkinter or pyusb missing
            raise unittest.SkipTest("viewer not importable: %s" % e)
        cls.viewer = viewer

    def state(self, now=100.0, mode=None, **kw):
        args = dict(disconnected=False, live=True, session_ok=False, live_refused=False,
                    refused_since=0.0, recover_until=0.0, last_frame=now)
        args.update(kw)
        return self.viewer.status_text(now, mode, **args)

    def test_video_mode_without_live_view(self):
        # the release does nothing in video mode, so "remote release armed" would mislead
        self.assertEqual(self.state(mode=self.viewer.MODE_VIDEO, live=False,
                                    session_ok=True),
                         "video mode - no stills; set the dial to shoot")
        self.assertEqual(self.state(mode=self.viewer.MODE_VIDEO, live=False,
                                    disconnected=True),
                         "camera disconnected - waiting")

    def test_order_of_precedence(self):
        self.assertEqual(self.state(disconnected=True, live_refused=True),
                         "camera disconnected - waiting")
        self.assertEqual(self.state(live=False), "arming remote release (session) ...")
        self.assertEqual(self.state(live=False, session_ok=True),
                         "remote release armed - press the body shutter")
        self.assertEqual(self.state(), "live")

    def test_refused(self):
        self.assertIn("Mq", self.state(mode=8, live_refused=True))
        self.assertIn("video", self.state(mode=10, live_refused=True))
        self.assertIn("too hot", self.state(live_refused=True, refused_since=50.0))
        self.assertIn("wake the screen", self.state(live_refused=True, refused_since=90.0))

    def test_recovering_and_stalled(self):
        self.assertIn("recovering", self.state(recover_until=101.0, last_frame=0.0))
        self.assertIn("check the camera", self.state(last_frame=90.0))
        self.assertIn("Mq", self.state(mode=8, last_frame=90.0))


class FocusPositionTest(unittest.TestCase):
    def test_range(self):
        self.assertEqual(params.focus_position(0.0), 0.0)
        self.assertEqual(params.focus_position(1.0), 1.0)
        self.assertAlmostEqual(params.focus_position(0.3817005545286507), 0.3817, 4)

    def test_not_available(self):
        self.assertIsNone(params.focus_position(-1.0))      # no session / no lens
        self.assertIsNone(params.focus_position(None))      # a failed read
        self.assertIsNone(params.focus_position("3fd86dc829a4331c"))
        self.assertIsNone(params.focus_position(1.5))



class LensTest(unittest.TestCase):
    def test_focal_length(self):
        self.assertEqual(params.focal_length(45), 45)
        self.assertEqual(params.focal_length("80"), 80)
        self.assertIsNone(params.focal_length(0))       # lens off
        self.assertIsNone(params.focal_length(-1))
        self.assertIsNone(params.focal_length(None))

    def test_focal_range(self):
        self.assertEqual(params.focal_range("MLi45,45,1"), (45, 45))
        self.assertEqual(params.focal_range("MLi40,85,1"), (40, 85))
        self.assertIsNone(params.focal_range("MLi0,0,1"))
        self.assertIsNone(params.focal_range(None))
        self.assertIsNone(params.focal_range("LI2,0,1"))

    def test_lens_name(self):
        self.assertEqual(params.lens_name("XCD 45P"), "XCD 45P")
        self.assertIsNone(params.lens_name(None))
        self.assertIsNone(params.lens_name("  "))


if __name__ == "__main__":
    unittest.main()
