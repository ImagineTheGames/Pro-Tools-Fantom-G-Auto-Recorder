#!/usr/bin/env python3
"""
audio.py - Measuring recorded audio.

You cannot listen to a capture that ran while you were out of the room, so
these measurements stand in for listening. They answer, in order of how often
the answer is surprising:

    is there anything here at all?   -> Take.has_audio
    where does it start?             -> Take.onset
    is it distorted?                 -> Take.clipping
    is it music, or hum?             -> Take.character

Thresholds are derived from each file's own noise floor rather than fixed dB
values. A fixed threshold fails both ways: set it low and it triggers on hiss,
set it high and quiet parts never register.
"""

import glob
import math
import os
import wave


def _db(x, full):
    return 20.0 * math.log10(x / full) if x > 0 else -999.0


class Take(object):
    """One recorded file, loaded once and measured many ways."""

    WINDOW_MS = 5.0
    FLOOR_PERCENTILE = 0.05
    TRIGGER_OVER_FLOOR_DB = 12.0

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        w = wave.open(path, "rb")
        self.frames = w.getnframes()
        width = w.getsampwidth()
        self.rate = w.getframerate()
        channels = w.getnchannels()
        raw = w.readframes(self.frames)
        w.close()

        step = 3 if width == 3 else 2
        self.full = float(2 ** 23 if width == 3 else 2 ** 15)
        self.samples = [int.from_bytes(raw[i:i + step], "little", signed=True)
                        for i in range(0, len(raw) - step + 1, step * channels)]
        self.duration = len(self.samples) / float(self.rate)
        self._windows = None

    # -- basics ------------------------------------------------------------

    @property
    def dc(self):
        return sum(self.samples) / float(len(self.samples)) if self.samples else 0.0

    @property
    def peak_db(self):
        return _db(max(abs(v) for v in self.samples), self.full) if self.samples else -999.0

    @property
    def rms_db(self):
        if not self.samples:
            return -999.0
        acc = 0.0
        for v in self.samples:
            acc += float(v) * v
        return _db(math.sqrt(acc / len(self.samples)), self.full)

    @property
    def crest_db(self):
        return self.peak_db - self.rms_db

    # -- windows -----------------------------------------------------------

    def windows(self):
        """RMS per short window, as a fraction of full scale."""
        if self._windows is not None:
            return self._windows
        size = max(1, int(self.rate * self.WINDOW_MS / 1000.0))
        out = []
        acc = 0.0
        n = 0
        for v in self.samples:
            acc += float(v) * v
            n += 1
            if n == size:
                out.append(math.sqrt(acc / n) / self.full)
                acc = 0.0
                n = 0
        self._windows = (out, size)
        return self._windows

    @property
    def noise_floor(self):
        levels, _ = self.windows()
        if not levels:
            return 0.0
        ordered = sorted(levels)
        return ordered[max(0, int(len(ordered) * self.FLOOR_PERCENTILE))]

    #: A take peaking above this is audio, whatever the relative test says.
    ABSOLUTE_FLOOR_DB = -55.0

    @property
    def has_audio(self):
        """
        Is there sound here?

        The relative test alone is not enough. It takes the 5th percentile as
        the noise floor, which only holds if the file contains silence -- on a
        take that plays continuously, that percentile IS quiet music, the
        threshold lands above the signal, and a perfectly good track reads as
        empty. So an absolute check runs first: anything peaking above
        -55 dBFS has audio, full stop.
        """
        if not self.samples:
            return False
        if self.peak_db > self.ABSOLUTE_FLOOR_DB:
            return True
        levels, _ = self.windows()
        if not levels:
            return False
        floor = self.noise_floor or 1e-9
        return max(levels) >= floor * (10 ** (self.TRIGGER_OVER_FLOOR_DB / 20.0)) * 2

    @property
    def onset(self):
        """Seconds until the signal rises clearly above its own noise floor."""
        levels, size = self.windows()
        if not levels or not self.has_audio:
            return None
        trigger = (self.noise_floor or 1e-9) * (10 ** (self.TRIGGER_OVER_FLOOR_DB / 20.0))
        for i, lv in enumerate(levels):
            if lv >= trigger:
                return (i * size) / float(self.rate)
        return None

    # -- transients --------------------------------------------------------

    HOP_MS = 2.0

    def profile(self, hop_ms=None):
        """(level in dB per hop, hop size in samples). Cached per hop size."""
        hop_ms = hop_ms or self.HOP_MS
        if not hasattr(self, "_profiles"):
            self._profiles = {}
        if hop_ms in self._profiles:
            return self._profiles[hop_ms]
        size = max(1, int(self.rate * hop_ms / 1000.0))
        out = []
        acc = 0.0
        n = 0
        for v in self.samples:
            acc += float(v) * v
            n += 1
            if n == size:
                out.append(_db(math.sqrt(acc / n), self.full))
                acc = 0.0
                n = 0
        self._profiles[hop_ms] = (out, size)
        return self._profiles[hop_ms]

    def noise_band(self, hop_ms=5.0, search_ms=1200.0, win_ms=80.0):
        """
        (centre, spread) of this file's noise, in dB.

        Measured from the quietest short stretch near the START, not from the
        quietest portion of the whole file. On a take that plays continuously
        the quietest third of the file is still music, which puts the estimate
        tens of decibels too high and the threshold above everything -- three
        of these takes reported no attack at all for exactly that reason.
        Every take here opens with silence, because recording rolls before the
        notes do, and that silence is the only honest sample of the noise.

        Median and median absolute deviation rather than mean and standard
        deviation, so one loud window cannot drag the estimate up.
        """
        db, _ = self.profile(hop_ms)
        if not db:
            return (-999.0, 0.0)
        win = max(3, int(win_ms / hop_ms))
        end = min(len(db), max(win, int(search_ms / hop_ms)))
        best = None
        for i in range(0, end - win + 1):
            seg = sorted(db[i:i + win])
            med = seg[len(seg) // 2]
            if best is None or med < best[0]:
                best = (med, db[i:i + win])
        if best is None:
            return (min(db), 0.0)
        centre, seg = best
        devs = sorted(abs(v - centre) for v in seg)
        return (centre, devs[len(devs) // 2])

    def transient(self, guard_ms=8.0, sustain_ms=30.0, margin_db=9.0, hop_ms=5.0):
        """
        Where the first attack begins, in seconds, or None if nothing attacks.

        Two things make this different from asking where the level crosses a
        line. First, the threshold comes from the noise's own spread rather
        than a fixed number of dB, because a file whose noise wanders over ten
        decibels needs a higher bar than one that sits still. Second, the level
        has to STAY up for sustain_ms: noise peaks last a window or two and
        notes do not, and without that rule the loudest crackle in the silence
        wins. Ten of twenty takes here triggered on noise before it was added.

        Having found the note, it then walks back DOWN the attack to its foot,
        because cutting where the level crossed the threshold would cut the
        attack off. guard_ms is subtracted after that: being a few
        milliseconds early costs nothing, being one millisecond late is
        audible.
        """
        db, size = self.profile(hop_ms)
        if len(db) < 8:
            return None
        centre, spread = self.noise_band(hop_ms)
        if max(db) - centre < 6.0:
            # Either silence throughout or a level that never changes. Saying
            # so is better than returning a confident wrong number.
            return None

        trigger = centre + max(margin_db, 3.0 * spread)
        need = max(2, int(sustain_ms / hop_ms))

        # An attack rises out of something quieter, so a run has to be preceded
        # by quiet to count. Without this, a file that opens mid-artefact -- a
        # punch-in click, a converter settling -- scores a run starting at
        # sample zero and the whole take reads as beginning at 0.000.
        before = max(2, int(20.0 / hop_ms))
        hit = None
        run = 0
        for i, v in enumerate(db):
            if v >= trigger:
                run += 1
                if run >= need:
                    start = i - run + 1
                    if start >= before and max(db[start - before:start]) < trigger:
                        hit = start
                        break
                    run = 0          # not an attack; keep looking
            else:
                run = 0
        if hit is None:
            return None

        # Walk back to the foot of the rise -- the last window before the climb
        # that was still down in the noise.
        limit = max(0, hit - int(250.0 / hop_ms))
        quiet = centre + 3.0
        foot = max(limit, hit - need)
        j = hit
        while j > limit:
            if db[j] <= quiet:
                foot = j
                break
            j -= 1

        t = (foot * size) / float(self.rate) - (guard_ms / 1000.0)
        return max(0.0, t)

    def tab_to_transient(self, snap_db=3.0, max_advance_ms=250.0, hop_ms=5.0):
        """
        Where Pro Tools' Tab to Transient lands, in seconds, or None.

        transient() finds where sound first rises out of the noise. That is not
        the same place, and the gap is audible: on the first take here it sat
        65 ms earlier than the manual edit, because a quiet precursor about
        8 dB below the note's real level came first and the noise-relative test
        took it. Tab to Transient measures against the material's OWN playing
        level, so it ignores that precursor and lands on the actual jump.

        So: bracket the note with transient(), which is reliable and consistent
        across takes, then step forward to the first point that comes within
        snap_db of the level this track actually plays at. The step forward is
        capped, so a sound that swells slowly keeps its swell rather than
        having the front of it cut away.
        """
        foot = self.transient(hop_ms=hop_ms)
        if foot is None:
            return None
        db, size = self.profile(hop_ms)
        per = size / float(self.rate)
        i0 = int(foot / per)

        # The playing level, measured from inside the music rather than near
        # its edge: a window that still holds silence drags the estimate down
        # and the threshold lands back in the noise. Five tracks fired up to
        # 200 ms early that way.
        lo = min(len(db), i0 + int(0.5 / per))
        hi = min(len(db), i0 + int(3.5 / per))
        seg = sorted(v for v in db[lo:hi] if v > -900)
        if not seg:
            return foot
        plateau = seg[len(seg) // 2]

        trigger = plateau - snap_db
        limit = min(len(db), i0 + int((max_advance_ms / 1000.0) / per))
        run = 0
        for i in range(i0, limit):
            if db[i] >= trigger:
                run += 1
                if run >= 2:
                    return max(0.0, (i - 1) * per)
            else:
                run = 0
        return foot

    # -- problems ----------------------------------------------------------

    @property
    def clipping(self):
        """(clipped samples, flat-topped runs, longest run)."""
        near = int(self.full * 0.999)
        clipped = runs = longest = current = 0
        for v in self.samples:
            if abs(v) >= near:
                clipped += 1
                current += 1
                longest = max(longest, current)
            else:
                if current >= 3:
                    runs += 1
                current = 0
        return clipped, runs, longest

    @property
    def character(self):
        """'music', 'static', 'silent' - is this a performance or a drone?"""
        if not self.has_audio:
            return "silent"
        levels, _ = self.windows()
        chunk = max(1, len(levels) // 12)
        means = []
        for i in range(0, len(levels) - chunk + 1, chunk):
            seg = levels[i:i + chunk]
            means.append(sum(seg) / len(seg))
        if len(means) < 3:
            return "music"
        lo, hi = min(means), max(means)
        return "music" if hi > lo * 3 else "static"

    def energy_map(self, buckets=40):
        levels, _ = self.windows()
        if not levels:
            return "." * buckets
        size = max(1, len(levels) // buckets)
        glyphs = []
        for b in range(buckets):
            seg = levels[b * size:(b + 1) * size]
            if not seg:
                glyphs.append(".")
                continue
            d = _db(max(seg), 1.0)
            if d <= -60:   glyphs.append(".")
            elif d <= -45: glyphs.append("▁")
            elif d <= -30: glyphs.append("▃")
            elif d <= -18: glyphs.append("▅")
            elif d <= -6:  glyphs.append("▇")
            else:          glyphs.append("█")
        return "".join(glyphs)

    def report(self):
        clipped, runs, longest = self.clipping
        lines = [
            "  duration    %.2f s @ %d Hz" % (self.duration, self.rate),
            "  peak        %.1f dBFS" % self.peak_db,
            "  rms         %.1f dBFS" % self.rms_db,
            "  crest       %.1f dB" % self.crest_db,
            "  dc offset   %.4f%% of full scale" % (100.0 * self.dc / self.full),
            "  onset       %s" % ("-" if self.onset is None else "%.3f s" % self.onset),
            "  character   %s" % self.character,
            "  clipping    %d sample(s), %d flat run(s), longest %d" % (clipped, runs, longest),
            "  energy      %s" % self.energy_map(),
        ]
        if runs and clipped:
            lines.append("  -> CLIPPING: reduce gain before the converter.")
        if self.character == "static":
            lines.append("  -> static spectrum: a drone or hum, not a performance.")
        if not self.has_audio:
            lines.append("  -> NO AUDIO above this file's own noise floor.")
        return "\n".join(lines)


def latest_takes(session, skip_prefixes=("ZZ",)):
    """{track name: newest Take} for a Pro Tools session's audio folder."""
    files = glob.glob(os.path.join(session, "**", "*.L.wav"), recursive=True)
    newest = {}
    for p in files:
        stem = os.path.basename(p).split("_")[0]
        if any(stem.upper().startswith(x.upper()) for x in skip_prefixes):
            continue
        if stem not in newest or os.path.getmtime(p) > os.path.getmtime(newest[stem]):
            newest[stem] = p
    return dict((k, Take(v)) for k, v in newest.items())
