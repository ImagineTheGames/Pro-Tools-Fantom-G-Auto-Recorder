#!/usr/bin/env python3
"""
spectrum.py - What frequencies is this file actually made of?

Distinguishes hum, a stuck drone, and real music. Mains hum concentrates at
50/60 Hz and its harmonics; a stuck note is one narrow peak that never moves;
music spreads energy and changes over time.

Usage:
    python spectrum.py file.wav [more.wav ...]
"""

import math
import os
import sys
import wave

import numpy as np


def load(path):
    w = wave.open(path, "rb")
    n, sw, sr, ch = w.getnframes(), w.getsampwidth(), w.getframerate(), w.getnchannels()
    raw = w.readframes(n)
    w.close()
    step = 3 if sw == 3 else 2
    full = float(2 ** 23 if sw == 3 else 2 ** 15)
    vals = np.array([int.from_bytes(raw[i:i + step], "little", signed=True)
                     for i in range(0, len(raw) - step + 1, step * ch)], dtype=np.float64)
    return vals / full, sr


def report(path):
    x, sr = load(path)
    if x.size == 0:
        print("  empty")
        return
    x = x - x.mean()

    n = min(len(x), sr * 8)
    seg = x[:n] * np.hanning(n)
    spec = np.abs(np.fft.rfft(seg))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    spec[0] = 0.0

    total = float(np.sum(spec ** 2)) or 1e-30
    order = np.argsort(spec)[::-1][:8]
    print("  dominant frequencies:")
    for i in order:
        share = 100.0 * float(spec[i] ** 2) / total
        if share < 0.05:
            continue
        print("     %8.1f Hz   %5.1f%% of energy" % (freqs[i], share))

    def band(lo, hi):
        m = (freqs >= lo) & (freqs < hi)
        return 100.0 * float(np.sum(spec[m] ** 2)) / total

    print("  band distribution:")
    for lo, hi, name in ((0, 40, "sub 40"), (40, 70, "40-70 (mains)"),
                         (70, 250, "70-250"), (250, 2000, "250-2k"),
                         (2000, 8000, "2k-8k"), (8000, sr / 2, "8k+")):
        print("     %-16s %5.1f%%" % (name, band(lo, hi)))

    # does the spectrum change over time? music does, hum doesn't
    frames = 12
    fl = len(x) // frames
    centroids = []
    for f in range(frames):
        s = x[f * fl:(f + 1) * fl]
        if len(s) < 64:
            continue
        sp = np.abs(np.fft.rfft(s * np.hanning(len(s))))
        fr = np.fft.rfftfreq(len(s), 1.0 / sr)
        e = float(np.sum(sp))
        centroids.append(float(np.sum(fr * sp) / e) if e > 0 else 0.0)
    if centroids:
        var = max(centroids) - min(centroids)
        print("  spectral centroid: %.0f Hz avg, range %.0f Hz over time"
              % (sum(centroids) / len(centroids), var))
        mains = band(40, 70)
        if mains > 40:
            print("  -> MAINS HUM. This is not the instrument.")
        elif var < 60:
            print("  -> static spectrum: a drone or stuck note, not a performance.")
        else:
            print("  -> spectrum evolves: real musical content.")


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
