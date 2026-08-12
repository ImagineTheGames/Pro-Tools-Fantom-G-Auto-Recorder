#!/usr/bin/env python3
"""
audio_path_test.py - Is the Fantom's audio actually reaching Pro Tools?

Records two short takes on the same track:
    A) silence  -- nothing sent
    B) signal   -- a loud sustained chord on a chosen channel

If B is not clearly louder than A, the analogue path is broken: MIDI may be
arriving at the synth, but its output is not reaching the interface, and
everything captured so far is whatever else is plugged into those inputs.

Usage:
    python audio_path_test.py [channel]      (default: sweep 1..16)
"""

import glob
import math
import os
import subprocess
import sys
import time
import wave

import mido

from usb_midi import RolandUsbMidiOut

PTOOLS = os.environ.get("PTOOLS_JS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ptools.js"))
SESSION = os.environ.get("PT_SESSION", ".")
TRACK = "ZZ PathTest"
HOLD = 3.0


def pt(*a):
    r = subprocess.run(["node", PTOOLS] + list(a), capture_output=True, text=True, timeout=40)
    return r.returncode == 0


def newest_wav(after):
    files = [p for p in glob.glob(os.path.join(SESSION, "**", "*.L.wav"), recursive=True)
             if os.path.getmtime(p) > after]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def level(path):
    w = wave.open(path, "rb")
    n, sw, ch = w.getnframes(), w.getsampwidth(), w.getnchannels()
    raw = w.readframes(n)
    w.close()
    step = 3 if sw == 3 else 2
    full = float(2 ** 23 if sw == 3 else 2 ** 15)
    peak = 0
    ss = 0.0
    c = 0
    for i in range(0, len(raw) - step + 1, step * ch):
        v = int.from_bytes(raw[i:i + step], "little", signed=True)
        a = abs(v)
        if a > peak:
            peak = a
        ss += float(v) * v
        c += 1
    rms = math.sqrt(ss / c) if c else 0.0
    to_db = lambda x: 20 * math.log10(x / full) if x > 0 else -999.0
    return to_db(peak), to_db(rms)


def take(port, notes, channel):
    """Record one take; play `notes` on `channel` if given."""
    pt("disarm-all")
    pt("record-arm", "--name", TRACK)
    pt("locate", "--samples", "0")
    mark = time.time()
    pt("record")
    time.sleep(0.4)
    if notes:
        for n in notes:
            port.send(mido.Message("note_on", channel=channel, note=n, velocity=120))
        time.sleep(HOLD)
        for n in notes:
            port.send(mido.Message("note_off", channel=channel, note=n, velocity=0))
    else:
        time.sleep(HOLD)
    time.sleep(0.4)
    pt("stop")
    time.sleep(1.2)
    w = newest_wav(mark)
    if not w:
        return None
    return level(w)


def main():
    pt("ensure-track", "--name", TRACK)
    port = RolandUsbMidiOut()
    print("Device: %s\n" % port.describe())

    with port:
        print("Recording SILENCE (nothing sent) ...")
        base = take(port, None, 0)
        if not base:
            sys.exit("No file produced -- is the track record-enabled?")
        print("  noise floor:  peak %.1f dBFS   rms %.1f dBFS\n" % base)

        chans = [int(sys.argv[1]) - 1] if len(sys.argv) > 1 else list(range(16))
        print("%-6s %10s %10s %10s   %s" % ("CH", "PEAK", "RMS", "vs FLOOR", "VERDICT"))
        print("-" * 6 + " " + "-" * 10 + " " + "-" * 10 + " " + "-" * 10 + "   " + "-" * 22)
        best = -999.0
        for ch in chans:
            res = take(port, [48, 55, 60, 64], ch)
            if not res:
                print("%-6d      (no file)" % (ch + 1))
                continue
            pk, rm = res
            delta = rm - base[1]
            if delta > best:
                best = delta
            verdict = "AUDIO PRESENT" if delta > 6 else "nothing"
            print("%-6d %10.1f %10.1f %+10.1f   %s" % (ch + 1, pk, rm, delta, verdict))

        print()
        if best <= 6:
            print("NO CHANNEL produced audio above the noise floor.")
            print("The Fantom's outputs are not reaching the interface. Check that its")
            print("OUTPUT jacks are cabled to the Scarlett inputs, and that the Scarlett")
            print("is the Pro Tools playback engine.")
        else:
            print("Audio path confirmed: best channel was %+.1f dB above the floor." % best)


if __name__ == "__main__":
    main()

