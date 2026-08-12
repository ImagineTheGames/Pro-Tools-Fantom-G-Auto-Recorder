#!/usr/bin/env python3
"""
diagnose_wav.py - Why does a recording sound wrong?

Distinguishes the usual suspects rather than guessing:
  * clipping        samples pinned at full scale, and flat-topped runs
  * DC offset       waveform not centred
  * level           peak / RMS, and crest factor
  * silence / gaps  how much of the file is near-silent
  * onset map       where energy actually occurs, so overlapping or
                    unexpected material shows up as a shape

Usage:
    python diagnose_wav.py "C:\\path\\file.wav" [more.wav ...]
"""

import math
import os
import sys
import wave


def read_samples(path):
    w = wave.open(path, "rb")
    n, sw, sr, ch = w.getnframes(), w.getsampwidth(), w.getframerate(), w.getnchannels()
    raw = w.readframes(n)
    w.close()
    step = 3 if sw == 3 else 2
    full = float(2 ** 23 if sw == 3 else 2 ** 15)
    vals = []
    for i in range(0, len(raw) - step + 1, step * ch):
        vals.append(int.from_bytes(raw[i:i + step], "little", signed=True))
    return vals, sr, full


def db(x, full):
    if x <= 0:
        return -999.0
    return 20 * math.log10(x / full)


def diagnose(path):
    vals, sr, full = read_samples(path)
    if not vals:
        print("  empty file")
        return

    peak = max(abs(v) for v in vals)
    mean = sum(vals) / float(len(vals))
    ss = 0.0
    for v in vals:
        ss += float(v) * v
    rms = math.sqrt(ss / len(vals))

    # clipping: at/near full scale, and flat-topped runs of identical extremes
    near = int(full * 0.999)
    clipped = sum(1 for v in vals if abs(v) >= near)
    runs = 0
    longest = 0
    cur = 0
    for v in vals:
        if abs(v) >= near:
            cur += 1
            if cur > longest:
                longest = cur
        else:
            if cur >= 3:
                runs += 1
            cur = 0

    print("  length      %.2f s @ %d Hz" % (len(vals) / float(sr), sr))
    print("  peak        %.1f dBFS" % db(peak, full))
    print("  rms         %.1f dBFS" % db(rms, full))
    print("  crest       %.1f dB" % (db(peak, full) - db(rms, full)))
    print("  dc offset   %.4f%% of full scale" % (100.0 * mean / full))
    print("  clipped     %d sample(s) at >=99.9%% (%.4f%%), %d flat run(s), longest %d"
          % (clipped, 100.0 * clipped / len(vals), runs, longest))

    if clipped and runs:
        print("  -> CLIPPING: flat-topped peaks. Reduce gain before the converter.")
    elif db(peak, full) > -1.0:
        print("  -> very hot but not flat-topped; little headroom left.")

    # energy map: 40 buckets so overlapping/unexpected material is visible
    buckets = 40
    size = max(1, len(vals) // buckets)
    print("  energy      ", end="")
    levels = []
    for b in range(buckets):
        seg = vals[b * size:(b + 1) * size]
        if not seg:
            levels.append(-999)
            continue
        p = max(abs(v) for v in seg)
        levels.append(db(p, full))
    for lv in levels:
        if lv <= -60:   c = "."
        elif lv <= -30: c = "\u2581"
        elif lv <= -18: c = "\u2583"
        elif lv <= -9:  c = "\u2585"
        elif lv <= -3:  c = "\u2587"
        else:           c = "\u2588"
        sys.stdout.write(c)
    print()
    quiet = sum(1 for lv in levels if lv <= -60)
    print("  near-silent %d of %d slices" % (quiet, buckets))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for p in sys.argv[1:]:
        print()
        print(os.path.basename(p))
        print("-" * 60)
        try:
            diagnose(p)
        except Exception as e:
            print("  failed: %s" % e)
    print()


if __name__ == "__main__":
    main()
