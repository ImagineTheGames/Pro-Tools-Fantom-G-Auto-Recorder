#!/usr/bin/env python3
"""
fantom_stem.py - Per-part stem capture for hardware synths.

Plays ONE part of a Standard MIDI File at a time out to a hardware synth, so
each part can be captured in isolation during a single continuous DAW pass.
Replaces the manual mute-everything-but-one workflow.

Isolation works by simply not sending the other parts' notes. Nothing on the
synth is muted, and no program changes are sent -- whatever Live Set / Studio
Set you already have loaded stays exactly as it is.

Typical use:
    python fantom_stem.py ports
    python fantom_stem.py inspect song.mid
    python fantom_stem.py plan song.mid
    python fantom_stem.py run song.mid --port "Fantom"

Then: arm one stereo track in the DAW, hit record, run the command, stop.
Split the recording at the bar positions in the generated cue sheet.
"""

import argparse
import csv
import ctypes
import os
import re
import sys
import time
from collections import OrderedDict

try:
    import mido
except ImportError:
    sys.exit("mido not installed.  Run:  python -m pip install mido python-rtmidi")


# ---------------------------------------------------------------- timing ----

# How long before a scheduled event we stop sleeping and start spinning.
# Windows' sleep granularity is coarse; the spin buys back sub-millisecond
# accuracy at the cost of a little CPU on the final approach only.
SPIN_MARGIN = 0.0015


def enable_hires_timer():
    """Ask Windows for 1ms scheduler granularity. Returns True if granted."""
    if os.name != "nt":
        return False
    try:
        return ctypes.windll.winmm.timeBeginPeriod(1) == 0
    except Exception:
        return False


def release_hires_timer():
    if os.name == "nt":
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass


def sleep_until(target, t0):
    """Block until perf_counter() - t0 >= target, accurately."""
    while True:
        remaining = target - (time.perf_counter() - t0)
        if remaining <= 0:
            return
        if remaining > SPIN_MARGIN:
            time.sleep(remaining - SPIN_MARGIN)
        else:
            while target - (time.perf_counter() - t0) > 0:
                pass
            return


# ------------------------------------------------------------ smf parsing ---

class Song:
    def __init__(self, path):
        if not os.path.isfile(path):
            here = sorted(f for f in os.listdir(".")
                          if f.lower().endswith((".mid", ".smf", ".midi")))
            msg = "No such file: %s" % path
            msg += ("\nMIDI files in this folder:\n  " + "\n  ".join(here)) if here \
                else "\nThere are no MIDI files in this folder yet."
            sys.exit(msg)
        self.path = path
        self.mid = mido.MidiFile(path)
        self.ppq = self.mid.ticks_per_beat
        self.tempo_map = self._build_tempo_map()
        self.tick_to_sec = self._make_tick_to_sec()
        self.numerator, self.denominator = self._time_signature()
        self.bar_ticks = int(self.ppq * 4 / self.denominator * self.numerator)
        self.parts = self._extract_parts()
        self.end_tick = max((p["max_tick"] for p in self.parts.values()), default=0)

    def _build_tempo_map(self):
        tempos = []
        for track in self.mid.tracks:
            t = 0
            for msg in track:
                t += msg.time
                if msg.type == "set_tempo":
                    tempos.append((t, msg.tempo))
        tempos.sort(key=lambda x: x[0])
        if not tempos or tempos[0][0] != 0:
            tempos.insert(0, (0, 500000))  # default 120bpm
        return tempos

    def _make_tick_to_sec(self):
        """Precompute seconds-at-each-tempo-change, then interpolate."""
        points = []
        sec = 0.0
        prev_tick, prev_tempo = self.tempo_map[0]
        points.append((prev_tick, 0.0, prev_tempo))
        for tick, tempo in self.tempo_map[1:]:
            sec += (tick - prev_tick) * prev_tempo / 1e6 / self.ppq
            points.append((tick, sec, tempo))
            prev_tick, prev_tempo = tick, tempo

        def convert(tick):
            lo, hi = 0, len(points) - 1
            while lo < hi:
                m = (lo + hi + 1) // 2
                if points[m][0] <= tick:
                    lo = m
                else:
                    hi = m - 1
            ptick, psec, ptempo = points[lo]
            return psec + (tick - ptick) * ptempo / 1e6 / self.ppq

        return convert

    def _time_signature(self):
        for track in self.mid.tracks:
            for msg in track:
                if msg.type == "time_signature":
                    return msg.numerator, msg.denominator
        return 4, 4

    def _extract_parts(self):
        """Group channel events by (track index, MIDI channel)."""
        names = {}
        for ti, track in enumerate(self.mid.tracks):
            for msg in track:
                if msg.type == "track_name":
                    names[ti] = msg.name.strip()
                    break

        parts = OrderedDict()
        for ti, track in enumerate(self.mid.tracks):
            t = 0
            for msg in track:
                t += msg.time
                if msg.is_meta:
                    continue
                ch = getattr(msg, "channel", None)
                if ch is None:
                    continue
                key = (ti, ch)
                if key not in parts:
                    parts[key] = {
                        "track": ti,
                        "channel": ch,
                        "name": names.get(ti) or "Track %d" % ti,
                        "events": [],
                        "notes": 0,
                        "programs": set(),
                        "ccs": set(),
                        "max_tick": 0,
                    }
                p = parts[key]
                p["events"].append((t, msg))
                p["max_tick"] = max(p["max_tick"], t)
                if msg.type == "note_on" and msg.velocity > 0:
                    p["notes"] += 1
                elif msg.type == "program_change":
                    p["programs"].add(msg.program)
                elif msg.type == "control_change":
                    p["ccs"].add(msg.control)
        return parts

    def bars(self, tick):
        return tick / self.bar_ticks if self.bar_ticks else 0

    def tempo_bpm(self):
        return 60_000_000 / self.tempo_map[0][1]


# ----------------------------------------------------------------- panic ----

def panic_messages(channel):
    """Everything needed to leave a channel completely silent and neutral."""
    return [
        mido.Message("control_change", channel=channel, control=64, value=0),   # sustain off
        mido.Message("control_change", channel=channel, control=120, value=0),  # all sound off
        mido.Message("control_change", channel=channel, control=123, value=0),  # all notes off
        mido.Message("control_change", channel=channel, control=121, value=0),  # reset controllers
        mido.Message("pitchwheel", channel=channel, pitch=0),
    ]


def panic_all(port):
    for ch in range(16):
        for msg in panic_messages(ch):
            port.send(msg)


# -------------------------------------------------------------- schedule ----

def select_parts(song, spec):
    """spec: None for all, or comma list of 1-based indices into the part list."""
    keys = list(song.parts.keys())
    if not spec:
        return keys
    chosen = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        i = int(token) - 1
        if i < 0 or i >= len(keys):
            sys.exit("No part %s (there are %d)." % (token, len(keys)))
        chosen.append(keys[i])
    return chosen


def region_events(part, start_tick, end_tick, send_programs):
    """
    Events for one part within [start_tick, end_tick), as (abs_tick, msg).

    Two details matter when you slice out the middle of an arrangement:

      * Controller chase. A CC or pitch bend set before the region still
        applies inside it. Without chasing, a part whose volume or mod wheel
        was set at bar 1 plays wrong from bar 9. The last value of each
        controller before the region is re-sent at the region start.

      * Held notes. A note that began before the region and is still sounding
        at its start gets re-triggered at the region start. Without this, a
        sustained pad whose note-on sits at bar 1 would be silent -- or worse,
        the track would look empty and get skipped entirely.

      * Hanging notes. A note still sounding at the region end gets an
        explicit note-off, so nothing drones into the next part's slot.
    """
    chase = OrderedDict()
    held = OrderedDict()
    inside = []
    sounding = set()

    for tick, msg in part["events"]:
        if msg.type == "program_change" and not send_programs:
            continue
        if tick < start_tick:
            if msg.type == "control_change":
                chase[("cc", msg.control)] = msg
            elif msg.type == "pitchwheel":
                chase[("pb",)] = msg
            elif msg.type == "program_change":
                chase[("pc",)] = msg
            elif msg.type == "note_on" and msg.velocity > 0:
                held[msg.note] = msg.velocity
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                held.pop(msg.note, None)
            continue
        if tick >= end_tick:
            break
        inside.append((tick, msg))
        if msg.type == "note_on" and msg.velocity > 0:
            sounding.add(msg.note)
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            sounding.discard(msg.note)

    out = [(start_tick, m) for m in chase.values()]
    for note, vel in held.items():
        out.append((start_tick, mido.Message("note_on", channel=part["channel"],
                                             note=note, velocity=vel)))
        sounding.add(note)
    out.extend(inside)
    for note in sorted(sounding):
        out.append((end_tick, mido.Message("note_off", channel=part["channel"],
                                           note=note, velocity=0)))
    out.sort(key=lambda e: e[0])
    return out


def build_schedule(song, keys, loops, gap_bars, lead_bars, loop_bars, send_programs,
                   region=None, include_empty=False):
    """
    Returns (events, cues).
      events: [(time_sec, mido.Message)] sorted, absolute seconds from pass start
      cues:   per-part dict describing where it lands in the recording

    `region` is an inclusive 1-based bar range, e.g. (9, 16) for bars 9-16.
    Without it the pass starts at bar 1.
    """
    if region:
        start_bar, end_bar = region
        start_tick = int((start_bar - 1) * song.bar_ticks)
        end_tick = int(end_bar * song.bar_ticks)
    else:
        start_tick = 0
        if loop_bars:
            end_tick = int(loop_bars * song.bar_ticks)
        else:
            # Round the song length to a whole bar. A note-off a few ticks past
            # the final bar line is normal and must NOT add a whole bar -- that
            # would append silence to every loop iteration and break the loop.
            exact = song.end_tick / float(song.bar_ticks)
            whole = int(exact)
            if exact - whole > 0.02:
                whole += 1
            end_tick = max(1, whole) * song.bar_ticks

    # seconds measured relative to the region start, so tempo changes before
    # the region don't shift everything
    base = song.tick_to_sec(start_tick)

    def rel(tick):
        return song.tick_to_sec(tick) - base

    loop_sec = rel(end_tick)
    gap_sec = rel(start_tick + int(gap_bars * song.bar_ticks))
    lead_sec = rel(start_tick + int(lead_bars * song.bar_ticks))

    events = []
    cues = []
    cursor = lead_sec

    skipped = []
    idx = 0

    for key in keys:
        part = song.parts[key]
        ev = region_events(part, start_tick, end_tick, send_programs)
        n_notes = sum(1 for _, m in ev if m.type == "note_on" and m.velocity > 0)

        # a track with nothing in this region is silence -- recording it wastes
        # pass time and leaves an empty stem to throw away later
        if n_notes == 0 and not include_empty:
            skipped.append(part["name"])
            continue

        idx += 1
        part_start = cursor
        slot_sec = loop_sec * loops

        for it in range(loops):
            offset = cursor + loop_sec * it
            for tick, msg in ev:
                events.append((offset + rel(tick), msg.copy()))

        # silence the channel at the end of the slot, before the gap
        quiet_at = cursor + slot_sec
        for msg in panic_messages(part["channel"]):
            events.append((quiet_at, msg))

        cues.append({
            "index": idx,
            "name": part["name"],
            "channel": part["channel"] + 1,
            "notes": n_notes,
            "start_sec": part_start,
            "keep_sec": part_start + loop_sec * (loops - 1),
            "end_sec": quiet_at,
        })

        cursor = quiet_at + gap_sec

    events.sort(key=lambda e: e[0])

    bar_sec = loop_sec / max(1, (end_tick - start_tick) / song.bar_ticks)
    for c in cues:
        c["start_bar"] = c["start_sec"] / bar_sec + 1
        c["keep_bar"] = c["keep_sec"] / bar_sec + 1

    return events, cues, cursor, (end_tick - start_tick) // song.bar_ticks, skipped


# ---------------------------------------------------------------- output ----

def fmt_time(sec):
    return "%d:%05.2f" % (int(sec // 60), sec % 60)


def print_cues(cues, total_sec, loop_bars, loops):
    print()
    print("  #  Part                     Ch   Notes   Starts at   Bar     KEEP from   Bar")
    print("  -- ------------------------ ---- ------- ----------- ------- ----------- -------")
    for c in cues:
        print("  %2d  %-24s %-4d %-7d %-11s %-7.1f %-11s %-7.1f" % (
            c["index"], c["name"][:24], c["channel"], c["notes"],
            fmt_time(c["start_sec"]), c["start_bar"],
            fmt_time(c["keep_sec"]), c["keep_bar"]))
    print()
    print("  Total pass length: %s   (%d parts x %d loops of %d bars)" % (
        fmt_time(total_sec), len(cues), loops, loop_bars))
    print()
    print("  KEEP column = start of the final loop iteration. Use that one -- by then")
    print("  reverb tails and delay feedback have reached steady state, so it sounds")
    print("  like what you hear when the loop is cycling.")


def write_cue_csv(path, cues):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["index", "part", "channel", "notes",
                    "start_sec", "start_bar", "keep_sec", "keep_bar", "end_sec"])
        for c in cues:
            w.writerow([c["index"], c["name"], c["channel"], c["notes"],
                        "%.4f" % c["start_sec"], "%.3f" % c["start_bar"],
                        "%.4f" % c["keep_sec"], "%.3f" % c["keep_bar"],
                        "%.4f" % c["end_sec"]])


# -------------------------------------------------------------- commands ----

def find_port(substr):
    names = mido.get_output_names()
    if not names:
        sys.exit("No MIDI output ports found.")
    if not substr:
        sys.exit("Specify --port (or --usb for raw USB). Available ports:\n  " +
                 "\n  ".join(names))
    matches = [n for n in names if substr.lower() in n.lower()]
    if not matches:
        sys.exit("No port matching %r. Available:\n  %s" % (substr, "\n  ".join(names)))
    if len(matches) > 1:
        sys.exit("%r is ambiguous:\n  %s" % (substr, "\n  ".join(matches)))
    return matches[0]


def open_output(args):
    """
    Open a MIDI destination. Returns (port, description).

    Two backends: a normal OS MIDI port via mido, or -- for devices with no
    usable Windows driver -- raw USB bulk transfers via libusb (--usb).
    Both expose .send() and work as context managers.
    """
    if getattr(args, "usb", False):
        try:
            from usb_midi import RolandUsbMidiOut, UsbMidiError
        except ImportError as e:
            sys.exit("USB backend unavailable: %s\nRun: python -m pip install pyusb libusb-package" % e)
        try:
            port = RolandUsbMidiOut()
        except UsbMidiError as e:
            sys.exit("USB backend: %s" % e)
        return port, port.describe()
    name = find_port(args.port)
    return mido.open_output(name), name


def cmd_ports(args):
    outs = mido.get_output_names()
    ins = mido.get_input_names()
    print("MIDI OUTPUTS:")
    print("\n".join("  " + n for n in outs) if outs else "  (none)")
    print("MIDI INPUTS:")
    print("\n".join("  " + n for n in ins) if ins else "  (none)")


def cmd_test(args):
    port, desc = open_output(args)
    print("Opening %s ..." % desc)
    with port:
        ch = args.channel - 1
        print("Playing a short run on channel %d. Listen for it on the synth." % args.channel)
        try:
            for note in (60, 64, 67, 72):
                port.send(mido.Message("note_on", channel=ch, note=note, velocity=100))
                time.sleep(0.25)
                port.send(mido.Message("note_off", channel=ch, note=note, velocity=0))
            time.sleep(0.3)
        finally:
            for msg in panic_messages(ch):
                port.send(msg)
    print("Done. If you heard nothing, check the synth's part/channel assignment.")


#: PTSL client. Sits beside this file by default; override with PTOOLS_JS.
PTOOLS_JS = os.environ.get(
    "PTOOLS_JS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ptools.js"))


def ptools(*args):
    """
    Drive Pro Tools over PTSL. Returns (ok, output).

    Failures are reported but never abort a capture -- losing the DAW
    automation is annoying, losing a pass mid-flight is worse.
    """
    import subprocess
    try:
        r = subprocess.run(["node", PTOOLS_JS] + list(args),
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout or "").strip() + (r.stderr or "").strip()
        return r.returncode == 0, out
    except Exception as e:
        return False, str(e)


def fmt_timecode(seconds, fps=30):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    f = int((seconds - int(seconds)) * fps)
    return "%02d:%02d:%02d:%02d" % (h, m, s, f)


def parse_channels(spec):
    """'1-16', '1,3,5', '2-4,10' -> [1, 3, 5, ...] (1-based, ordered, unique)."""
    if not spec:
        return list(range(1, 17))
    out = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo, hi = token.split("-", 1)
            lo, hi = int(lo), int(hi)
            if lo > hi:
                lo, hi = hi, lo
            out.extend(range(lo, hi + 1))
        else:
            out.append(int(token))
    bad = [c for c in out if not 1 <= c <= 16]
    if bad:
        sys.exit("Channel(s) out of range 1-16: %s" % ", ".join(str(b) for b in bad))
    seen, ordered = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def cmd_sweep(args):
    """Play a short figure on each channel so you can hear which parts respond."""
    channels = parse_channels(args.channels)
    port, desc = open_output(args)
    print("  Port: %s" % desc)
    print("  Sweeping %d channel(s). Listen for which ones sound.\n" % len(channels))

    with port:
        try:
            for ch in channels:
                # channel 10 is percussion by convention; middle C is a poor
                # probe there, so use kick / snare / hat instead
                notes = (36, 38, 42) if ch == 10 else (
                    args.note, args.note + 7, args.note + 12)
                label = "drums" if ch == 10 else "notes %d,%d,%d" % notes
                print("    channel %2d  (%s) ..." % (ch, label), end="", flush=True)
                for n in notes:
                    port.send(mido.Message("note_on", channel=ch - 1,
                                           note=n, velocity=args.velocity))
                    time.sleep(args.hold)
                    port.send(mido.Message("note_off", channel=ch - 1, note=n, velocity=0))
                print(" sent")
                time.sleep(args.gap)
        except KeyboardInterrupt:
            print("\n  Interrupted.")
        finally:
            panic_all(port)

    print("\n  Done. Tell me which channels sounded and what they were.")


def mmc_message(command):
    """MIDI Machine Control, as a universal real-time SysEx."""
    codes = {"stop": 0x01, "play": 0x02, "deferred_play": 0x03,
             "fast_forward": 0x04, "rewind": 0x05, "record": 0x06,
             "record_exit": 0x07, "pause": 0x09, "reset": 0x0D}
    return mido.Message("sysex", data=[0x7F, 0x7F, 0x06, codes[command]])


def cmd_transport(args):
    """Send transport commands to the synth's sequencer."""
    port, desc = open_output(args)
    print("  Port: %s" % desc)

    with port:
        if args.mmc:
            mapping = {"start": "play", "stop": "stop", "continue": "deferred_play"}
            cmd = mapping.get(args.action, args.action)
            msg = mmc_message(cmd)
            print("  Sending MMC %s: %s" % (
                cmd.upper(), " ".join("%02X" % b for b in msg.bytes())))
            port.send(msg)
        else:
            if args.action == "start" and args.songpos is not None:
                sp = mido.Message("songpos", pos=args.songpos)
                print("  Sending Song Position Pointer -> beat %d" % args.songpos)
                port.send(sp)
                time.sleep(0.05)
            msg = mido.Message(args.action)
            print("  Sending real-time %s (0x%02X)" % (
                args.action.upper(), msg.bytes()[0]))
            port.send(msg)

            if args.hold > 0 and args.action in ("start", "continue"):
                # A slaved sequencer advances on clock, not on Start alone.
                interval = 60.0 / args.bpm / 24.0
                print("  Now clocking at %.1f BPM for %.1f s (%d pulses)..." % (
                    args.bpm, args.hold, int(args.hold / interval)))
                enable_hires_timer()
                clock = mido.Message("clock")
                t0 = time.perf_counter()
                n = 0
                try:
                    while True:
                        target = n * interval
                        if target > args.hold:
                            break
                        sleep_until(target, t0)
                        port.send(clock)
                        n += 1
                except KeyboardInterrupt:
                    print("  Interrupted.")
                finally:
                    release_hires_timer()
                print("  Sent %d clock pulses. Sending Stop." % n)
                port.send(mido.Message("stop"))

    print()
    print("  If the sequencer didn't move, check the Fantom's Sync settings:")
    print("    - Sync Mode must be SLAVE or REMOTE, not MASTER/INTERNAL")
    print("    - Sync Source (or Clock Source) must be USB, not MIDI")
    print("  That second one is the usual culprit -- a unit set to sync from the")
    print("  DIN jack will ignore everything we send over USB.")


def clock_events(song, total, start_stop=False, bpm=None):
    """
    MIDI clock at the standard 24 pulses per quarter note across the pass,
    so the synth's arpeggiator / RPS run in time with what we're sending.

    Start/Stop is OFF by default and should stay that way. An arpeggiator only
    needs clock to hold tempo. Start (0xFA) additionally launches the synth's
    own SEQUENCER when it's set to slave sync -- which plays the whole song
    underneath the isolated part and ruins the capture. Only enable it if you
    actually want the synth's sequencer running.

    Tempo comes from the SMF unless `bpm` overrides it. In SLAVE-MIDI mode the
    synth takes its tempo from this clock, so this is what the captured audio
    will actually run at -- the tempo stored on the synth is ignored.

    Pulses are placed from a tick counter rather than by accumulating a float
    interval, so there's no drift over a long pass.
    """
    step = max(1, song.ppq // 24)
    if bpm:
        pulse = 60.0 / bpm / 24.0

        def at(i):
            return i * pulse
    else:
        def at(i):
            return song.tick_to_sec(i * step)

    events = []
    if start_stop:
        events.append((0.0, mido.Message("start")))
    i = 0
    while True:
        t = at(i)
        if t > total:
            break
        events.append((t, mido.Message("clock")))
        i += 1
    if start_stop:
        events.append((total, mido.Message("stop")))
    return events


def cmd_inspect(args):
    song = Song(args.file)
    print()
    print("  File:        %s" % os.path.basename(song.path))
    print("  Format:      %d, %d track(s), %d PPQ" % (
        song.mid.type, len(song.mid.tracks), song.ppq))
    print("  Tempo:       %.2f BPM%s" % (
        song.tempo_bpm(),
        "  (%d tempo changes)" % (len(song.tempo_map) - 1) if len(song.tempo_map) > 1 else ""))
    print("  Time sig:    %d/%d  (%d ticks per bar)" % (
        song.numerator, song.denominator, song.bar_ticks))
    print("  Length:      %.2f bars / %s" % (
        song.bars(song.end_tick), fmt_time(song.tick_to_sec(song.end_tick))))
    print()
    print("  #  Part                     Ch   Notes   Bars   PC     CCs used")
    print("  -- ------------------------ ---- ------- ------ ------ ------------------")
    for i, (key, p) in enumerate(song.parts.items(), start=1):
        pcs = ",".join(str(x) for x in sorted(p["programs"])) or "-"
        ccs = ",".join(str(x) for x in sorted(p["ccs"])[:6]) or "-"
        if len(p["ccs"]) > 6:
            ccs += ",..."
        print("  %2d  %-24s %-4d %-7d %-6.1f %-6s %s" % (
            i, p["name"][:24], p["channel"] + 1, p["notes"],
            song.bars(p["max_tick"]), pcs, ccs))
    print()

    empties = [i for i, (_, p) in enumerate(song.parts.items(), 1) if p["notes"] == 0]
    if empties:
        print("  Note: part(s) %s contain no notes (controller data only)." %
              ", ".join(str(e) for e in empties))
    thin = [i for i, (_, p) in enumerate(song.parts.items(), 1) if 0 < p["notes"] <= 4]
    if thin:
        print("  Heads up: part(s) %s have very few notes. If any are driven by the" %
              ", ".join(str(t) for t in thin))
        print("  synth's arpeggiator or RPS, the SMF holds only the trigger chord --")
        print("  the pattern itself won't play back. Check those before a full pass.")
    print()


def parse_region(spec):
    """'9-16' -> (9, 16); '9' -> (9, 9). Inclusive, 1-based bars."""
    if not spec:
        return None
    if "-" in spec:
        a, b = spec.split("-", 1)
        lo, hi = int(a), int(b)
    else:
        lo = hi = int(spec)
    if lo < 1 or hi < lo:
        sys.exit("Bad --region %r. Use e.g. 9-16 (1-based, inclusive)." % spec)
    return (lo, hi)


def report_skipped(skipped):
    if not skipped:
        return
    print("  Skipped %d track(s) with no notes in this region:" % len(skipped))
    shown = ", ".join(skipped[:8])
    if len(skipped) > 8:
        shown += ", ... (+%d more)" % (len(skipped) - 8)
    print("    %s" % shown)
    print("  Use --include-empty to record them anyway.")
    print()


def cmd_plan(args):
    song = Song(args.file)
    keys = select_parts(song, args.parts)
    events, cues, total, loop_bars, skipped = build_schedule(
        song, keys, args.loops, args.gap, args.lead, args.bars, args.send_programs,
        region=parse_region(args.region), include_empty=args.include_empty)
    clocks = 0
    if args.clock:
        ticks = clock_events(song, total, start_stop=args.clock_start, bpm=args.clock_bpm)
        clocks = len(ticks)
        events = sorted(events + ticks, key=lambda e: e[0])
    print_cues(cues, total, loop_bars, args.loops)
    print()
    report_skipped(skipped)
    out = os.path.splitext(args.file)[0] + "_cues.csv"
    write_cue_csv(out, cues)
    print("  Cue sheet written to %s" % out)
    print("  %d MIDI events scheduled%s." % (
        len(events), " (%d of them clock)" % clocks if clocks else ""))
    print()


def run_per_track(song, keys, args):
    """
    One Pro Tools track per part, each recorded as its own take from timeline
    zero. Slower than a single continuous pass -- a record start/stop per part
    -- but it lands 20 named, stacked stems ready to mix instead of one long
    file to carve up, and every stem shares the same start point.
    """
    region = parse_region(args.region)
    port, desc = open_output(args)
    print("  Port: %s" % desc)
    print("  Mode: one Pro Tools track per part\n")

    enable_hires_timer()
    made = 0
    try:
        with port:
            panic_all(port)
            for idx, key in enumerate(keys, start=1):
                part = song.parts[key]
                events, cues, total, loop_bars, skipped = build_schedule(
                    song, [key], args.loops, 0, args.lead, args.bars,
                    args.send_programs, region=region, include_empty=args.include_empty)
                if not cues:
                    print("  %2d/%d  %-22s (no notes in region, skipped)"
                          % (idx, len(keys), part["name"]))
                    continue
                if args.clock:
                    events = sorted(events + clock_events(
                        song, total, start_stop=args.clock_start, bpm=args.clock_bpm),
                        key=lambda e: e[0])

                name = re.sub(r"[^A-Za-z0-9 _-]", "", part["name"]).strip()[:24]
                track = "%02d %s" % (idx, name or ("Part %d" % idx))

                ptools("ensure-track", "--name", track)
                ptools("disarm-all")
                ptools("record-arm", "--name", track)
                ptools("locate", "--samples", "0")
                # 'record' already waits for the transport to be genuinely
                # rolling, so the pre-roll here only needs to cover converter
                # latency -- not several seconds of dead air at the top.
                ptools("record")
                time.sleep(args.pt_preroll)

                # Whatever happens to the MIDI, Pro Tools must be stopped. A
                # failure mid-part previously left it recording indefinitely.
                failed = None
                try:
                    t0 = time.perf_counter()
                    for when, msg in events:
                        sleep_until(when, t0)
                        port.send(msg)
                    sleep_until(total, t0)
                    # Let the tail ring. Only release held notes (CC 123) --
                    # All Sound Off (CC 120) would cut the reverb we're here
                    # to capture. Keep recording through it.
                    port.send(mido.Message("control_change",
                                           channel=part["channel"], control=64, value=0))
                    port.send(mido.Message("control_change",
                                           channel=part["channel"], control=123, value=0))
                    time.sleep(args.tail)
                except Exception as e:
                    failed = e
                finally:
                    ptools("stop")
                    for m in panic_messages(part["channel"]):
                        port.send(m)

                if failed is not None:
                    print("  %2d/%d  %-22s FAILED: %s"
                          % (idx, len(keys), part["name"], failed))
                    print("         Pro Tools stopped. Aborting the pass.")
                    break
                made += 1
                print("  %2d/%d  %-22s ch%-3d %s  -> %s"
                      % (idx, len(keys), part["name"], part["channel"] + 1,
                         fmt_time(total), track))
                time.sleep(0.35)
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        ptools("stop")
    finally:
        release_hires_timer()
        ptools("disarm-all")

    print("\n  Done. %d track(s) recorded, each starting at timeline zero." % made)


def cmd_run(args):
    song = Song(args.file)
    keys = select_parts(song, args.parts)
    if args.per_track:
        return run_per_track(song, keys, args)

    events, cues, total, loop_bars, skipped = build_schedule(
        song, keys, args.loops, args.gap, args.lead, args.bars, args.send_programs,
        region=parse_region(args.region), include_empty=args.include_empty)
    if not cues:
        sys.exit("Nothing to record -- no selected track has notes in this region.")
    if args.clock:
        events = sorted(events + clock_events(song, total, start_stop=args.clock_start,
                                              bpm=args.clock_bpm),
                        key=lambda e: e[0])
    port, desc = open_output(args)

    print_cues(cues, total, loop_bars, args.loops)
    print()
    report_skipped(skipped)
    out = os.path.splitext(args.file)[0] + "_cues.csv"
    write_cue_csv(out, cues)
    print("  Cue sheet: %s" % out)
    print("  Port:      %s" % desc)
    print()

    if not args.yes:
        try:
            input("  Arm and record in your DAW, then press Enter to start (Ctrl+C aborts)... ")
        except KeyboardInterrupt:
            print("\n  Aborted.")
            return

    hires = enable_hires_timer()
    if not hires:
        print("  (Could not raise timer resolution; timing may be coarser.)")

    # Roll Pro Tools before the MIDI so the recording starts first. The offset
    # between record-start and MIDI-start is measured, not assumed, so the
    # markers land where the audio actually is.
    pt_offset = 0.0
    if args.protools:
        ok, out = ptools("record-arm", "--name", args.pt_track)
        print("  Pro Tools: arm %s -> %s" % (args.pt_track, "ok" if ok else out))
        rec_at = time.perf_counter()
        ok, out = ptools("record")
        print("  Pro Tools: record rolling -> %s" % ("ok" if ok else out))
        time.sleep(args.pt_preroll)
        pt_offset = time.perf_counter() - rec_at

    late_total = 0.0
    late_max = 0.0
    sent = 0

    with port:
        panic_all(port)
        time.sleep(0.05)
        print("  Rolling. %s of audio.\n" % fmt_time(total))
        t0 = time.perf_counter()
        cue_i = 0
        try:
            for when, msg in events:
                sleep_until(when, t0)
                actual = time.perf_counter() - t0
                drift = actual - when
                if drift > 0:
                    late_total += drift
                    late_max = max(late_max, drift)
                port.send(msg)
                sent += 1
                while cue_i < len(cues) and cues[cue_i]["start_sec"] <= actual:
                    c = cues[cue_i]
                    print("    [%s]  part %d/%d  %-24s ch%d" % (
                        fmt_time(c["start_sec"]), c["index"], len(cues),
                        c["name"][:24], c["channel"]))
                    cue_i += 1
            sleep_until(total, t0)
        except KeyboardInterrupt:
            print("\n  Interrupted -- silencing all channels.")
        finally:
            panic_all(port)
            release_hires_timer()

    elapsed = time.perf_counter() - t0

    if args.protools:
        ok, out = ptools("stop")
        print("\n  Pro Tools: stopped -> %s" % ("ok" if ok else out))
        made = 0
        for c in cues:
            at = pt_offset + c["keep_sec"]
            samples = int(round(at * args.pt_rate))
            ok, _ = ptools("marker", "--name", c["name"][:31],
                           "--samples", str(samples))
            made += 1 if ok else 0
        print("  Pro Tools: %d/%d marker(s) placed at the KEEP positions" % (made, len(cues)))
        if pt_offset:
            print("  (recording began %.2f s before the first note; markers offset to match)"
                  % pt_offset)

    print()
    print("  Done. %d messages in %s (scheduled %s)." % (sent, fmt_time(elapsed), fmt_time(total)))
    if sent:
        print("  Timing: mean lateness %.3f ms, worst %.3f ms." % (
            late_total / sent * 1000, late_max * 1000))
    print()
    print("  Stop recording. Split at the KEEP positions in the cue sheet.")


# ------------------------------------------------------------------ main ----

def main():
    ap = argparse.ArgumentParser(
        description="Per-part stem capture for hardware synths.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports", help="list MIDI ports").set_defaults(func=cmd_ports)

    t = sub.add_parser("test", help="play a short run to verify the connection")
    t.add_argument("--port", help="substring of the MIDI output port name")
    t.add_argument("--usb", action="store_true",
                   help="talk to the Fantom directly over raw USB (needs WinUSB bound)")
    t.add_argument("--channel", type=int, default=1, help="MIDI channel 1-16 (default 1)")
    t.set_defaults(func=cmd_test)

    s = sub.add_parser("sweep", help="play a figure on each channel to find which parts respond")
    s.add_argument("--port", help="substring of the MIDI output port name")
    s.add_argument("--usb", action="store_true", help="talk to the Fantom directly over raw USB")
    s.add_argument("--channels", help="e.g. 1-16, 1,3,5, or 2 (default all 16)")
    s.add_argument("--note", type=int, default=60, help="root note (default 60 = middle C)")
    s.add_argument("--velocity", type=int, default=100, help="velocity (default 100)")
    s.add_argument("--hold", type=float, default=0.35, help="seconds per note (default 0.35)")
    s.add_argument("--gap", type=float, default=0.5, help="seconds between channels (default 0.5)")
    s.set_defaults(func=cmd_sweep)

    tr = sub.add_parser("transport", help="send Start/Stop/Continue to the sequencer")
    tr.add_argument("action", choices=["start", "stop", "continue"])
    tr.add_argument("--port", help="substring of the MIDI output port name")
    tr.add_argument("--usb", action="store_true", help="talk to the Fantom directly over raw USB")
    tr.add_argument("--mmc", action="store_true",
                    help="use MIDI Machine Control SysEx instead of real-time messages")
    tr.add_argument("--songpos", type=int, default=None,
                    help="send a Song Position Pointer (in beats) before Start")
    tr.add_argument("--hold", type=float, default=0,
                    help="after Start, keep sending clock for N seconds (a slaved "
                         "sequencer advances on clock, not on Start alone)")
    tr.add_argument("--bpm", type=float, default=120.0,
                    help="tempo for the clock sent by --hold (default 120)")
    tr.set_defaults(func=cmd_transport)

    i = sub.add_parser("inspect", help="analyse an SMF")
    i.add_argument("file")
    i.set_defaults(func=cmd_inspect)

    def common(p):
        p.add_argument("file")
        p.add_argument("--parts", help="comma list of part numbers, e.g. 1,3,7 (default all)")
        p.add_argument("--loops", type=int, default=3,
                       help="loop iterations per part; keep the last (default 3)")
        p.add_argument("--gap", type=float, default=2,
                       help="bars of silence between parts, for tails (default 2)")
        p.add_argument("--lead", type=float, default=1,
                       help="bars of silence before the first part (default 1)")
        p.add_argument("--bars", type=float, default=None,
                       help="loop length in bars from bar 1 (default: auto from file length)")
        p.add_argument("--region",
                       help="capture a bar range instead, e.g. 9-16 (1-based, inclusive). "
                            "Controllers set before the region are chased in.")
        p.add_argument("--include-empty", action="store_true",
                       help="also record tracks with no notes in the region "
                            "(skipped by default)")
        p.add_argument("--send-programs", action="store_true",
                       help="send program changes (default: off, keeps your loaded set)")
        p.add_argument("--clock", action="store_true",
                       help="send MIDI clock + Start/Stop through the pass, so the "
                            "synth's arpeggiator and RPS run in sync")
        p.add_argument("--clock-bpm", type=float, default=None,
                       help="override the clock tempo (default: taken from the SMF). "
                            "In SLAVE-MIDI mode this is the tempo the audio records at.")
        p.add_argument("--clock-start", action="store_true",
                       help="also send MIDI Start/Stop. WARNING: with the synth in "
                            "slave sync this launches ITS sequencer, playing the whole "
                            "song under the isolated part. Rarely what you want.")

    pl = sub.add_parser("plan", help="preview the pass and write a cue sheet, send nothing")
    common(pl)
    pl.set_defaults(func=cmd_plan)

    r = sub.add_parser("run", help="perform the capture pass")
    common(r)
    r.add_argument("--port", help="substring of the MIDI output port name")
    r.add_argument("--usb", action="store_true",
                   help="talk to the Fantom directly over raw USB (needs WinUSB bound)")
    r.add_argument("--yes", action="store_true", help="skip the Enter prompt")
    r.add_argument("--protools", action="store_true",
                   help="drive Pro Tools over PTSL: arm, record, stop, and drop "
                        "a named marker at every stem's KEEP position")
    r.add_argument("--pt-track", default="Fantom Stems",
                   help="Pro Tools track to arm (default 'Fantom Stems')")
    r.add_argument("--pt-preroll", type=float, default=1.0,
                   help="seconds to record before the first note (default 1.0)")
    r.add_argument("--pt-rate", type=int, default=48000,
                   help="session sample rate, for marker placement (default 48000)")
    r.add_argument("--per-track", action="store_true",
                   help="record each part onto its OWN Pro Tools track, all starting "
                        "at timeline zero, instead of one continuous pass on a single "
                        "track. Creates and names a track per part. Implies --protools.")
    r.add_argument("--tail", type=float, default=4.0,
                   help="seconds to keep recording after the last note, so reverb and "
                        "delay tails are captured (per-track mode, default 4.0)")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
