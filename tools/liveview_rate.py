#!/usr/bin/env python3
"""Measure the live-view frame rate and bitrate the camera delivers per read strategy.

Phocus receives ~22 fps from the same body that gives the tether app ~11, at the same
size per frame. Frame size is the encoder's budget; frame rate is what makes motion
smear or not. This compares ways of reading endpoint 0x84:

  app        one 256 KB read at a time, timeout 80 ms - what viewer.py does
  big        one 2.7 MB read at a time on a dedicated thread, nothing else in the loop
  dual       two threads reading 2.7 MB concurrently - two transfers always queued,
             which is what Phocus does

  python3 tools/liveview_rate.py [seconds-per-mode] [mode ...]

Starts live view, measures each mode, stops live view. Changes no settings.
"""
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import hbctl, liveview  # noqa: E402

BIG = 2770944                      # the transfer size Phocus uses on 0x84


def video_frames(chunk):
    """(count, total payload bytes) of video frames in a byte string."""
    frames, _ = liveview.frames_from(chunk)
    return len(frames), sum(len(liveview.payload(f)) for f in frames)


def measure(cam, mode, seconds):
    stop = time.time() + seconds
    lock = threading.Lock()
    stats = {"frames": 0, "bytes": 0, "reads": 0, "times": []}
    buf = [b""]

    def record(n, size):
        with lock:
            stats["frames"] += n
            stats["bytes"] += size
            stats["reads"] += 1
            stats["times"].extend([time.time()] * n)

    def reader(size, timeout, whole_reads):
        while time.time() < stop:
            try:
                chunk = bytes(cam.dev.read(hbctl.EP_DATA, size, timeout=timeout))
            except Exception:
                continue
            if whole_reads:            # one transfer = one LIVE packet: parse it alone
                record(*video_frames(chunk))
            else:                      # stream: frames may straddle reads
                with lock:
                    frames, buf[0] = liveview.frames_from(buf[0] + chunk)
                record(len(frames), sum(len(liveview.payload(f)) for f in frames))

    if mode == "app":
        threads = [threading.Thread(target=reader, args=(1 << 18, 80, False))]
    elif mode == "big":
        threads = [threading.Thread(target=reader, args=(BIG, 200, True))]
    elif mode == "dual":
        threads = [threading.Thread(target=reader, args=(BIG, 200, True)) for _ in range(2)]
    else:
        raise SystemExit("unknown mode %r" % mode)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    times = sorted(stats["times"])
    gaps = [b - a for a, b in zip(times, times[1:])]
    fps = stats["frames"] / seconds
    print("%-5s %5.1f fps  %6.0f KB/frame  %5.1f Mbit/s  %4d reads  gap median %.0f ms, max %.0f ms"
          % (mode, fps, stats["bytes"] / max(1, stats["frames"]) / 1024,
             stats["bytes"] * 8 / seconds / 1e6, stats["reads"],
             1000 * statistics.median(gaps) if gaps else 0, 1000 * max(gaps) if gaps else 0),
          flush=True)


def main():
    args = sys.argv[1:]
    seconds = float(args.pop(0)) if args and args[0].replace(".", "").isdigit() else 10.0
    modes = args or ["app", "big", "dual", "app"]
    with hbctl.Camera(timeout=800) as cam:
        r = cam.start_live()
        if not r or r[1] != b"\x00\x00\x00\x00":
            raise SystemExit("live view refused (ack %s) - wake the screen, exit menus"
                             % (r[1].hex(" ") if r else "none"))
        try:
            time.sleep(1.0)
            cam.flush_stream(0.5)
            for mode in modes:
                measure(cam, mode, seconds)
        finally:
            cam.stop_live()


if __name__ == "__main__":
    main()
