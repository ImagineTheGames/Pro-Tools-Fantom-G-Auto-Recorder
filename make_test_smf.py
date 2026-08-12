#!/usr/bin/env python3
"""Generate a synthetic 8-bar, multi-part SMF for testing fantom_stem.py."""

import mido
from mido import Message, MidiFile, MidiTrack, MetaMessage, bpm2tempo

PPQ = 480
BAR = PPQ * 4
BARS = 8


def add(track, events):
    """events: [(abs_tick, Message)] -> append with correct delta times."""
    events.sort(key=lambda e: e[0])
    prev = 0
    for tick, msg in events:
        msg.time = tick - prev
        track.append(msg)
        prev = tick


mid = MidiFile(type=1, ticks_per_beat=PPQ)

conductor = MidiTrack()
conductor.append(MetaMessage("track_name", name="Conductor", time=0))
conductor.append(MetaMessage("set_tempo", tempo=bpm2tempo(96), time=0))
conductor.append(MetaMessage("time_signature", numerator=4, denominator=4, time=0))
conductor.append(MetaMessage("end_of_track", time=BAR * BARS))
mid.tracks.append(conductor)

# Bass - straight eighths on the root, ch 1
bass = MidiTrack()
bass.append(MetaMessage("track_name", name="Bass", time=0))
ev = []
roots = [36, 36, 43, 41]
for bar in range(BARS):
    root = roots[bar % 4]
    for eighth in range(8):
        t = bar * BAR + eighth * (PPQ // 2)
        ev.append((t, Message("note_on", channel=0, note=root, velocity=100)))
        ev.append((t + PPQ // 3, Message("note_off", channel=0, note=root, velocity=0)))
add(bass, ev)
mid.tracks.append(bass)

# Keys - held chords with a mod wheel sweep, ch 2
keys = MidiTrack()
keys.append(MetaMessage("track_name", name="Keys Pad", time=0))
ev = []
chords = [(60, 64, 67), (60, 64, 67), (62, 65, 69), (59, 62, 65)]
for bar in range(BARS):
    chord = chords[bar % 4]
    t = bar * BAR
    for n in chord:
        ev.append((t, Message("note_on", channel=1, note=n, velocity=72)))
        ev.append((t + BAR - 20, Message("note_off", channel=1, note=n, velocity=0)))
    for step in range(4):
        ev.append((t + step * PPQ,
                   Message("control_change", channel=1, control=1,
                           value=min(127, bar * 12 + step * 8))))
add(keys, ev)
mid.tracks.append(keys)

# Drums - kick/snare/hat, ch 10
drums = MidiTrack()
drums.append(MetaMessage("track_name", name="Drums", time=0))
ev = []
for bar in range(BARS):
    base = bar * BAR
    for beat in range(4):
        t = base + beat * PPQ
        if beat in (0, 2):
            ev.append((t, Message("note_on", channel=9, note=36, velocity=110)))
            ev.append((t + 60, Message("note_off", channel=9, note=36, velocity=0)))
        else:
            ev.append((t, Message("note_on", channel=9, note=38, velocity=100)))
            ev.append((t + 60, Message("note_off", channel=9, note=38, velocity=0)))
        for half in range(2):
            th = t + half * (PPQ // 2)
            ev.append((th, Message("note_on", channel=9, note=42, velocity=70)))
            ev.append((th + 40, Message("note_off", channel=9, note=42, velocity=0)))
add(drums, ev)
mid.tracks.append(drums)

# Lead - melody with a program change and pitch bend, ch 3
lead = MidiTrack()
lead.append(MetaMessage("track_name", name="Lead Synth", time=0))
ev = [(0, Message("program_change", channel=2, program=81))]
melody = [72, 74, 76, 79, 76, 74, 72, 69]
for bar in range(BARS):
    t = bar * BAR
    n = melody[bar % len(melody)]
    ev.append((t, Message("note_on", channel=2, note=n, velocity=95)))
    ev.append((t + PPQ * 2, Message("note_off", channel=2, note=n, velocity=0)))
    ev.append((t + PPQ, Message("pitchwheel", channel=2, pitch=1200)))
    ev.append((t + PPQ * 2 - 10, Message("pitchwheel", channel=2, pitch=0)))
add(lead, ev)
mid.tracks.append(lead)

# Sparse part - only a held chord, as an arpeggiator trigger would be, ch 4
arp = MidiTrack()
arp.append(MetaMessage("track_name", name="Arp Trigger", time=0))
ev = []
for n in (48, 52, 55):
    ev.append((0, Message("note_on", channel=3, note=n, velocity=90)))
    ev.append((BAR * BARS - 20, Message("note_off", channel=3, note=n, velocity=0)))
add(arp, ev)
mid.tracks.append(arp)

mid.save("test_song.mid")
print("Wrote test_song.mid: %d tracks, %d bars at 96 BPM" % (len(mid.tracks), BARS))
