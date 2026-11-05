#!/usr/bin/env python3
"""Command-line front end for the camera library.

  python3 tools/hbctl.py list
  python3 tools/hbctl.py read FirmwareVersion Battery
  python3 tools/hbctl.py write Iso 400
  python3 tools/hbctl.py download captures --session

The real implementation lives in lib/hbctl.py; this only makes it runnable.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.hbctl import main   # noqa: E402

if __name__ == "__main__":
    main()
