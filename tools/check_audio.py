#!/usr/bin/env python3
"""Report length, peak and RMS for the most recent recordings in a session."""

import glob
import math
import os
import sys
import wave

SESSION = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PT_SESSION", ".")
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 10


def analyse(path, stride=7):
    w = wave.open(path, "rb")
    n, sw, sr = w.getnframes(), w.getsampwidth(), w.getframerate()
    raw = w.readframes(n)
    w.close()
    step = 3 if sw == 3 else 2
    full = float(2 ** 23 if sw == 3 else 2 ** 15)
    peak = 0
    ss = 0.0
    c = 0
    for i in range(0, len(raw) - step + 1, step * stride):
        v = int.from_bytes(raw[i:i + step], "little", signed=True)
        a = abs(v)
        if a > peak:
            peak = a
        ss += float(v) * v
        c += 1
    rms = math.sqrt(ss / c) if c else 0.0
    to_db = lambda x: 20 * math.log10(x / full) if x > 0 else -999.0
    return n / float(sr), to_db(peak), to_db(rms)


def main():
    files = glob.glob(os.path.join(SESSION, "**", "*.wav"), recursive=True)
    files.sort(key=os.path.getmtime, reverse=True)
    files = files[:LIMIT]
    if not files:
        print("No .wav files under %s" % SESSION)
        return 1

    print("%-32s %8s %9s %9s   %s" % ("FILE", "LENGTH", "PEAK", "RMS", "VERDICT"))
    print("-" * 32 + " " + "-" * 8 + " " + "-" * 9 + " " + "-" * 9 + "   " + "-" * 14)
    silent = 0
    for p in files:
        try:
            dur, pk, rm = analyse(p)
            verdict = "SIGNAL"
            if pk <= -60:
                verdict = "*** SILENT ***"
                silent += 1
            print("%-32s %7.2fs %8.1f %8.1f   %s" % (
                os.path.basename(p), dur, pk, rm, verdict))
        except Exception as e:
            print("%-32s  %s" % (os.path.basename(p), e))
    print()
    print("%d file(s), %d silent." % (len(files), silent))
    return 0


if __name__ == "__main__":
    sys.exit(main())

