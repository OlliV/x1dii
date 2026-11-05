#!/usr/bin/env python3
"""Live view window for the X1D II, with no Phocus involved.

Starts the camera with the recorded connect sequence, keeps the `op 4` poll alive
(the camera stops streaming without it), then pipes the H.264 payload of every LIVE
frame into ffplay, which owns the window and does the decoding.

  python3 liveapp.py [--seconds N] [--record out.h264]

Live view is one command — `op 1` / code 0x0007 / first byte 0x0c — and all zeros stops
it. No handshake and no polling; the camera streams at ~14 fps on its own.
"""
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import hbctl              # noqa: E402
from lib import liveview as ls     # noqa: E402


def main():
    seconds = 60.0
    if "--seconds" in sys.argv:
        seconds = float(sys.argv[sys.argv.index("--seconds") + 1])
    record = None
    if "--record" in sys.argv:
        record = sys.argv[sys.argv.index("--record") + 1]

    cam = hbctl.Camera(timeout=800)
    try:
        # Live view is a single command: op 1 / 0x0007 / first byte 0x0c.
        # All zeros stops it. No handshake, no polling, no descriptors needed.
        r = cam.call(hbctl.OP_CMD, 0x0007, bytes([0x0C]) + bytes(9))
        print("live view started (ack %s); opening window"
              % (r[1].hex(" ") if r else "none"), flush=True)

        player = subprocess.Popen(
            ["ffplay", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
             "-framedrop", "-f", "h264", "-window_title", "X1D II live view", "-i", "-"],
            stdin=subprocess.PIPE)

        rec = open(record, "wb") if record else None
        buf = b""
        frames = 0
        bad = 0
        synced = False          # wait for an SPS so the decoder starts clean
        t0 = time.time()
        end = t0 + seconds
        try:
            while time.time() < end:
                try:
                    buf += bytes(cam.dev.read(hbctl.EP_DATA, 1 << 18, timeout=80))
                except Exception:
                    pass
                new, buf = ls.frames_from(buf)
                for f in new:
                    plen = int.from_bytes(f[12:16], "little")
                    payload = f[24:24 + plen]
                    if not synced:
                        if b"\x00\x00\x01\x67" not in payload:
                            continue
                        synced = True
                    if rec:
                        rec.write(payload)
                    try:
                        player.stdin.write(payload)
                        player.stdin.flush()
                    except (BrokenPipeError, ValueError):
                        bad += 1
                        break
                    frames += 1
                if frames and frames % 50 == 0 and frames % 50 == 0:
                    el = time.time() - t0
                    print("  %d frames  %.1f fps" % (frames, frames / el), flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            el = time.time() - t0
            print("\n%d frames in %.1fs (%.1f fps)%s" % (
                frames, el, frames / el if el else 0,
                ", %d write errors" % bad if bad else ""))
            if rec:
                rec.close()
                print("recorded to %s" % record)
            try:
                cam.call(hbctl.OP_CMD, 0x0007, bytes(10), retries=0)   # stop live view
            except Exception:
                pass
            try:
                player.stdin.close()
            except Exception:
                pass
            player.terminate()
    finally:
        cam.close()


if __name__ == "__main__":
    main()
