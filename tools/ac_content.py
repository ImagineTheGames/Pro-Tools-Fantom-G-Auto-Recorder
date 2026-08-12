#!/usr/bin/env python3
"""
ac_content.py - Is there real audio here, or just DC and noise?

Peak and RMS can both look healthy on a file that is inaudible: a large DC
offset carries "level" without carrying sound. This strips DC, then reports
what is left, plus a zero-crossing rate -- near-zero crossings with non-zero
RMS means the file is a DC step, not audio.

Usage:
    python ac_content.py file.wav [more.wav ...]
"""

import math
import os
import sys
import wave


def load(path):
    w = wave.open(path, "rb")
    n, sw, sr, ch = w.getnframes(), w.getsampwidth(), w.getframerate(), w.getnchannels()
    raw = w.readframes(n)
    w.close()
    step = 3 if sw == 3 else 2
    full = float(2 ** 23 if sw == 3 else 2 ** 15)
    vals = [int.from_bytes(raw[i:i + step], "little", signed=True)
            for i in range(0, len(raw) - step + 1, step * ch)]
    return vals, sr, full


def db(x, full):
    return 20 * math.log10(x / full) if x > 0 else -999.0


def report(path):
    vals, sr, full = load(path)
    if not vals:
        print("  empty")
        return
    n = len(vals)
    dc = sum(vals) / float(n)

    ac = [v - dc for v in vals]
    peak_ac = max(abs(v) for v in ac)
    ss = 0.0
    for v in ac:
        ss += v * v
    rms_ac = math.sqrt(ss / n)

    crossings = 0
    for i in range(1, n):
        if (ac[i - 1] < 0) != (ac[i] < 0):
            crossings += 1
    zcr = crossings / (n / float(sr))

    print("  duration        %.2f s" % (n / float(sr)))
    print("  DC offset       %.1f dBFS (%.4f%% of full scale)"
          % (db(abs(dc), full), 100.0 * dc / full))
    print("  AC peak         %.1f dBFS" % db(peak_ac, full))
    print("  AC rms          %.1f dBFS" % db(rms_ac, full))
    print("  zero crossings  %.0f /s" % zcr)

    if db(peak_ac, full) < -70:
        print("  -> NO AUDIO. Level is DC offset, not sound.")
    elif zcr < 20:
        print("  -> almost no oscillation; this is not musical audio.")
    else:
        print("  -> real audio present.")

    # where the audio actually is, DC removed
    buckets = 40
    size = max(1, n // buckets)
    out = []
    for b in range(buckets):
        seg = ac[b * size:(b + 1) * size]
        if not seg:
            out.append(".")
            continue
        p = max(abs(v) for v in seg)
        d = db(p, full)
        if d <= -70:   out.append(".")
        elif d <= -45: out.append("▁")
        elif d <= -30: out.append("▃")
        elif d <= -18: out.append("▅")
        elif d <= -6:  out.append("▇")
        else:          out.append("█")
    print("  AC energy       " + "".join(out))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for p in sys.argv[1:]:
        print()
        print(os.path.basename(p))
        print("-" * 62)
        try:
            report(p)
        except Exception as e:
            print("  failed: %s" % e)
    print()


if __name__ == "__main__":
    main()
