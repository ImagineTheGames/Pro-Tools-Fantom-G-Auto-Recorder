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
import collections
import csv
import ctypes
import glob
import json
import math
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
        self.end_note_tick = max(
            (p.get("max_note_tick", 0) for p in self.parts.values()), default=0)

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
                    # Where the last note STARTS, which is what sets the loop
                    # length. The last event is usually a note-off well past
                    # the final bar line, and treating that as musical length
                    # adds a whole bar of tail to every loop iteration.
                    p["max_note_tick"] = max(p.get("max_note_tick", 0), t)
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

def parse_parts(spec, count):
    """
    Part numbers from a spec, 1-based and in order.

    Accepts single numbers, ranges, and open ranges in any combination:

        7          just part 7
        17-        part 17 to the end -- capture the rest, skip what came before
        5-8        parts 5 to 8
        -4         parts 1 to 4
        1,3,9-12   any mixture

    Ranges matter because the common case is re-capturing the tail of a song
    after fixing a few sounds, and listing a dozen numbers by hand is a good
    way to fat-finger one.
    """
    if not spec or not str(spec).strip():
        return list(range(1, count + 1))

    picked = []
    for token in str(spec).split(","):
        token = token.strip()
        if not token:
            continue
        m = re.match(r"^(\d*)\s*-\s*(\d*)$", token)
        if m:
            lo = int(m.group(1)) if m.group(1) else 1
            hi = int(m.group(2)) if m.group(2) else count
        elif token.isdigit():
            lo = hi = int(token)
        else:
            sys.exit("Cannot read part spec %r. Use 7, 17-, 5-8, -4, or 1,3,9-12."
                     % token)
        if lo < 1 or hi > count or lo > hi:
            sys.exit("Part range %r is outside 1-%d." % (token, count))
        picked.extend(range(lo, hi + 1))

    seen, out = set(), []
    for n in picked:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def select_parts(song, spec):
    """spec: None for all, or a part spec -- see parse_parts."""
    keys = list(song.parts.keys())
    return [keys[n - 1] for n in parse_parts(spec, len(keys))]


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
            end_tick = max(1, song_loop_bars(song)) * song.bar_ticks

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


PTOOLS_JS = r"C:\Users\Rei\protools-mcp-server\ptools.js"


def ptools(*args):
    """
    Drive Pro Tools over PTSL, one-shot. Returns (ok, output).

    Convenient but SLOW and, worse, inconsistently slow: each call spawns a
    node process. Use PTools() for anything inside a capture loop.
    """
    import subprocess
    try:
        r = subprocess.run(["node", PTOOLS_JS] + list(args),
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout or "").strip() + (r.stderr or "").strip()
        return r.returncode == 0, out
    except Exception as e:
        return False, str(e)


class PTools(object):
    """
    A persistent PTSL connection.

    Node takes 150-300ms to start and that cost varies run to run. Spawning it
    per command puts that variance between "Pro Tools is recording" and "the
    MIDI starts" -- so every take lands at a slightly different offset and the
    stems drift out of sync with each other. Keeping one process alive for the
    whole pass removes it: commands then cost about a millisecond.
    """

    def __init__(self, js=None):
        import subprocess
        self.proc = subprocess.Popen(
            ["node", js or PTOOLS_JS, "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        hello = self._read()
        self.ready = bool(hello and hello.get("ok"))

    def _read(self):
        line = self.proc.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except ValueError:
            return None

    def __call__(self, cmd, **kw):
        if self.proc.poll() is not None:
            return {"ok": False, "error": "ptools exited"}
        kw["cmd"] = cmd
        try:
            self.proc.stdin.write(json.dumps(kw) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return self._read() or {"ok": False, "error": "no reply"}

    def close(self):
        try:
            self(("quit"))
        except Exception:
            pass
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


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


def cmd_markers(args):
    """List the session's memory locations, and optionally remove them."""
    from protools import Session, ProToolsError
    with Session() as pt:
        marks = pt.markers()
        if not marks:
            print("  No memory locations in the session.")
            return

        print()
        print("  %-6s %-26s %14s  %s" % ("NUM", "NAME", "START", "TYPE"))
        print("  " + "-" * 6 + " " + "-" * 26 + " " + "-" * 14 + "  " + "-" * 12)
        for m in marks:
            print("  #%-5s %-26s %14s  %s" % (
                m["number"], m["name"][:26], m["start"],
                (m.get("type") or "").replace("TP_", "")))
        print("\n  %d memory location(s)." % len(marks))

        if not args.clear:
            print("  Add --clear to remove them.")
            return

        # Only the ones this tool made, unless told otherwise. Everything it
        # writes is named after the part it marks.
        if args.all:
            doomed = marks
        else:
            doomed = [m for m in marks if re.match(r"^\s*Track \d+\s*$", m["name"] or "")]
            skipped = len(marks) - len(doomed)
            if skipped:
                print("  Keeping %d marker(s) this tool did not create; "
                      "use --all to remove those too." % skipped)
        if not doomed:
            print("  Nothing to remove.")
            return

        if not args.yes:
            print("\n  About to delete %d marker(s). Re-run with --yes to do it."
                  % len(doomed))
            return

        try:
            r = pt.clear_markers([m["number"] for m in doomed])
        except ProToolsError as e:
            sys.exit("  %s" % e)
        print("\n  Removed %d marker(s); %d left in the session."
              % (r["cleared"], r["remaining"]))


def cmd_verify(args):
    """Measure every recorded stem, since you can't listen to an unattended pass."""
    from audio import latest_takes
    takes = latest_takes(args.session)
    if not takes:
        sys.exit("No .L.wav files under %s" % args.session)

    print()
    print("  %-22s %8s %8s %7s %7s  %-8s %s" % (
        "TRACK", "PEAK", "RMS", "CREST", "ONSET", "CHAR", "ISSUE"))
    print("  " + "-" * 22 + " " + "-" * 8 + " " + "-" * 8 + " " + "-" * 7 + " " +
          "-" * 7 + "  " + "-" * 8 + " " + "-" * 24)
    bad = 0
    for name in sorted(takes):
        t = takes[name]
        clipped, runs, _ = t.clipping
        issue = ""
        if not t.has_audio:
            issue = "SILENT"
        elif runs and clipped:
            issue = "CLIPPING (%d flat runs)" % runs
        elif t.character == "static":
            issue = "static - hum or drone?"
        elif t.peak_db > -1.0:
            issue = "very hot"
        if issue:
            bad += 1
        print("  %-22s %8.1f %8.1f %7.1f %7s  %-8s %s" % (
            name[:22], t.peak_db, t.rms_db, t.crest_db,
            "-" if t.onset is None else "%.3f" % t.onset, t.character, issue))
    print()
    print("  %d take(s), %d needing attention." % (len(takes), bad))
    if args.detail:
        for name in sorted(takes):
            print()
            print("  " + name)
            print(takes[name].report())
    print()


def cmd_tab(args):
    """
    Tab to transient, separate, delete the head, pull the clip to zero.

    Pro Tools does the transient detection. PTSL exposes no Tab to Transient,
    so this used to measure the audio here and guess where Pro Tools would
    land. It does not work: tuned until it matched one track's manual edit, it
    cut into the attack of every other track. The MCP server drives Pro Tools'
    own menu commands, so the answer is Pro Tools' answer.
    """
    from ptmcp import ProToolsMcp, McpError
    from protools import Session

    # Pro Tools knows which session is open; there is no reason to be told.
    if not args.session:
        args.session = session_path_from_protools()

    with Session() as pt:
        tracks = sorted(
            (n for n, cl in pt.clips().items() if cl),
            key=lambda s: (int(re.match(r"^(\d+)", s).group(1))
                           if re.match(r"^(\d+)", s) else 999, s))
    if args.tracks:
        tracks = [t for t in tracks if t in args.tracks]
    if not tracks:
        sys.exit("No tracks with clips in this session.")

    try:
        with ProToolsMcp() as mcp:
            # Every check in here fails silently at runtime, so a pass that
            # looks like it worked can have done nothing at all.
            pre = mcp.preflight()
            print()
            print(pre.strip())
            if "Preflight OK" not in pre:
                sys.exit("\n  Automation cannot run right now. Fix the above first.")

            # A warning here is not advisory. A focused floating window
            # swallows the Tab keystroke; Tab then does nothing, and the
            # transient comes back as wherever the insertion already was. That
            # produced cuts ranging from 21 ms to 0.99 s on one recording, and
            # every one of them looked like a successful trim.
            # A dry run edits nothing, so a warning must not block it -- being
            # unable to look is worse than useless when something has gone
            # wrong and you are trying to find out what.
            if args.dry_run:
                print()
                print(mcp.trim_heads(tracks, max_head=args.max_head,
                                     dry_run=True).strip())
                print("\n  Dry run. Re-run without --dry-run to apply.")
                return

            # Warnings are printed, not obeyed. Refusing to trim over a
            # floating window left a whole capture untrimmed and the pass
            # carried on regardless, which was worse than the risk.

            print()
            done = 0
            for name in tracks:
                # One track at a time, checked after each. Trimming all twenty
                # and looking afterwards means a broken run damages all twenty.
                with Session() as pt:
                    before = pt.clip_span(name)
                out = mcp.trim_heads([name], max_head=args.max_head)
                pos = parse_transient(out, name)
                with Session() as pt:
                    after = pt.clip_span(name)

                if pos is None:
                    print("  %-22s no transient - left alone" % name[:22])
                    continue

                if "skipped" in out:
                    print("  %-22s transient %7d - server skipped it" % (name[:22], pos))
                    continue

                removed = before - after
                if abs(removed - pos) > 2:
                    print("\n  %s: Pro Tools reported a transient at %d but the "
                          "clip lost %d samples." % (name, pos, removed))
                    sys.exit("  Stopping before this damages the rest of the "
                             "session. %d track(s) were trimmed." % done)
                done += 1
                print("  %-22s transient %7d  removed %7d  ok" % (name[:22], pos, removed))

            print("\n  %d of %d track(s) trimmed and verified." % (done, len(tracks)))
    except McpError as e:
        sys.exit("\n  MCP server: %s" % e)

    with Session() as pt:
        ex = pt.extents()
    late = {k: v[0] for k, v in ex.items() if v[0] != 0}
    if late:
        print("  NOT at zero: %s" % late)


def parse_transient(text, name):
    """The transient position the server reported for one track, or None."""
    for ln in text.splitlines():
        if name in ln:
            m = re.search(r"transient (\d+)|trimmed at (\d+)", ln)
            if m:
                return int(m.group(1) or m.group(2))
    return None


def first_clip_len(clips, track, fallback):
    """
    Length of the first clip on a track -- the take, not the whole extent.

    Anything sitting after it is leftover from an earlier pass and has nothing
    to do with this recording.
    """
    cs = [c for c in clips.get(track, []) if c[0].endswith(".L")]
    if not cs:
        cs = clips.get(track, [])
    if not cs:
        return fallback
    return min(cs, key=lambda c: c[1])[3]


def bars_for_tick(song, tick):
    """
    Whole bars covering a note that STARTS at `tick`.

    The bar containing the note counts, so this is floor + 1: a last note
    beginning exactly on the downbeat of bar 16 makes a 16 bar loop, not 15.
    Songs whose final note falls a little before the line hid that off-by-one
    -- 7.94 bars rounds to 8 either way -- and a part starting its last note
    on the downbeat came back a whole bar short.
    """
    if tick <= 0:
        return 1
    return max(1, int(tick // song.bar_ticks) + 1)


def part_loop_bars(song, key):
    """
    How long THIS part loops, in bars: a whole multiple of the song's base.

    Parts in one song do not have to share a length -- a song can have two
    16 bar parts over a bed of 8 bar ones -- and forcing one length either
    truncates the long parts or gives the short ones half an iteration of
    silence.

    A shorter part is kept only when it divides the base evenly -- 4 into 8 is
    a four bar loop that repeats twice, and looping it at 8 would leave four
    bars of silence in every iteration. A length that fits neither way, like
    5 against a base of 8, is a part that stops early rather than a 5 bar
    loop, and looping it at 5 would drag it out of time; those round up.

    Everything therefore stays a whole multiple or divisor of the base, which
    is what keeps the parts aligned with each other on the timeline.

    Halving is as short as it goes. A part holding one hit near the start
    measures a single bar, and repeating that every bar turns one crash into
    eight; a sparse part is far likelier than a one bar loop. Use --bars, or
    B in the console, for a song where that is genuinely wrong.
    """
    base = song_loop_bars(song)
    own = bars_for_tick(song, song.parts[key].get("max_note_tick", 0))
    if own >= base:
        return base * int(math.ceil(own / float(base)))
    if base % own == 0 and own * 2 >= base:
        return own
    return base


def song_loop_bars(song):
    """
    The loop length in bars: where the music repeats, not where sound stops.

    Measured from the last note ON. A held chord or a long release runs past
    the final bar line as note-offs -- this song measures 8.12 bars that way --
    and rounding that up gave a 9 bar loop for 8 bars of music. Every loop
    iteration then carried a bar of tail, heard as a bar of silence appearing
    in music that never had one, and the tail is what --tail already covers.

    Capture and extend both come through here, so the length they use cannot
    drift apart.

    The base is the length MOST parts share, not the longest. Taking the
    longest lets two 16 bar parts stretch the other thirty-four to 16 and give
    each of them eight bars of silence per iteration.
    """
    lengths = [bars_for_tick(song, p.get("max_note_tick", 0))
               for p in song.parts.values() if p.get("max_note_tick")]
    if not lengths:
        return max(1, bars_for_tick(song, song.end_note_tick or song.end_tick))
    common = collections.Counter(lengths).most_common(1)[0][0]
    return max(1, common)


def loop_samples(song, bars, rate):
    """Samples in `bars` bars of this song, at the session's sample rate."""
    beat = 60.0 / song.tempo_bpm()
    bar_seconds = beat * (4.0 / song.denominator) * song.numerator
    return int(round(rate * bar_seconds * bars))


def cmd_extend(args):
    """
    Repeat the second loop until the session is long enough to work with.

    A capture is two loops: the first still has reverb and delay building up,
    the second is the loop as it sounds once it has settled. So the second one
    is what gets repeated -- bars 9-16 of an 8-bar part, and so on.

    Loop length comes from the MIDI's own tempo, not from the Pro Tools ruler.
    PTSL cannot set session tempo, so the ruler is usually still at whatever
    the session was created with and its bar lines are in the wrong place.
    """
    from protools import Session

    song = Song(args.file)
    with Session() as pt:
        rate = 48000
        try:
            sr = str(pt.info().get("sample_rate", {}).get("sample_rate", ""))
            digits = "".join(c for c in sr if c.isdigit())
            if digits:
                rate = int(digits)
        except Exception:
            pass

        # --bars comes through as a float from `run` and an int from `extend`.
        bars = int(args.bars) if args.bars else song_loop_bars(song)
        loop = loop_samples(song, bars, rate)
        target = int(args.minutes * 60 * rate)
        need = int(math.ceil(target / float(loop)))

        ex = pt.extents()
        clips = pt.clips()
        tracks = sorted(k for k, v in ex.items() if v[1] > 0)
        if not tracks:
            sys.exit("No tracks with clips in this session.")
        # The SHORTEST track decides how much is already down. Going by the
        # longest lets one over-extended track convince this that every track
        # is finished, and the rest stay at their recorded length.
        #
        # Rounded, not floored: trimming the head takes a fraction of a second
        # off, so two recorded loops measure 1.98 and floor called that one.
        shortest = min(first_clip_len(clips, k, ex[k][1] - ex[k][0])
                       for k in tracks)
        have = int(round(shortest / float(loop)))

        print()
        print("  loop      %d bars at %.2f BPM = %d samples (%.2f s)"
              % (bars, song.tempo_bpm(), loop, loop / float(rate)))
        print("  target    %.2f min -> %d loops (%.1f s)"
              % (args.minutes, need, need * loop / float(rate)))
        print("  have      %d loop(s) across %d track(s)" % (have, len(tracks)))

        # Group the tracks by how long THEIR part loops. A song can hold two
        # 16 bar parts over a bed of 8 bar ones; repeating everything at one
        # spacing would either double up the long parts or leave the short
        # ones half empty. Whole multiples stay aligned on the timeline.
        keys = list(song.parts.keys())
        groups = {}
        for k in tracks:
            head = k.split()[0]
            n = int(head) if head.isdigit() else 0
            if args.bars:
                b = int(args.bars)
            elif 1 <= n <= len(keys):
                b = part_loop_bars(song, keys[n - 1])
            else:
                b = bars
            groups.setdefault(b, []).append(k)

        if len(groups) > 1:
            print("  lengths   %s" % ", ".join(
                "%d bars x%d" % (b, len(v)) for b, v in sorted(groups.items())))

        if have >= need:
            print("\n  Already long enough.")
            return
        if args.dry_run:
            print("\n  Would paste up to %d more loop(s). Re-run without --dry-run."
                  % (need - have))
            return

        # Measure every group in units of the SHORTEST one, so the lengths are
        # exact whole multiples of each other. Rounding each group's loop from
        # the tempo separately cannot line up: an 8 bar loop of 1123891
        # samples is odd, so a 4 bar loop is 561945.5 and the two drift apart
        # by a sample every iteration.
        unit_bars = min(groups)
        unit = loop_samples(song, unit_bars, rate)
        widest = max(groups) // unit_bars          # units in the longest loop

        # Round the target up to something every group can reach exactly, so
        # they all finish on the same sample.
        need_units = int(math.ceil((args.minutes * 60 * rate) / float(unit)))
        need_units = int(math.ceil(need_units / float(widest))) * widest
        target_len = need_units * unit

        for b, group in sorted(groups.items()):
            mult = b // unit_bars
            g_loop = unit * mult
            g_need = need_units // mult

            # The take is the FIRST clip, not the track's extent. A track can
            # carry leftovers from an earlier pass sitting after the take, and
            # measuring start-of-first to end-of-last counted those as part of
            # the recording -- a 27.8 s take read as 51.2 s, the loop length
            # was inferred from the wrong number, and every paste came from
            # the wrong place.
            recorded = min(first_clip_len(clips, k, ex[k][1] - ex[k][0])
                           for k in group)
            g_have = int(round(recorded / float(g_loop)))
            if g_have >= g_need:
                continue
            rec_loop = min([g_loop, loop], key=lambda c: abs(recorded - 2 * c))

            # With only one iteration recorded there is no settled pass to
            # copy, so copy the one there is.
            settled = recorded >= 2 * rec_loop - g_loop // 2
            src = ((rec_loop, rec_loop + g_loop) if settled else (0, g_loop))
            pt.send("copy-range", tracks=group, **{"in": src[0], "out": src[1]})

            # Repeats begin where the recorded loops end -- a take of two
            # 4 bar loops is bars 1-8, so the first paste lands on bar 9.
            #
            # Clear from there to the end of the track first. What sits past
            # the last whole loop is the ring-out tail, and sometimes a clip
            # left over from an earlier pass; pasting around them leaves a
            # ragged few bars between the take and the first repeat.
            start_at = g_have * g_loop
            for k in group:
                end = ex[k][1]
                if end > start_at:
                    pt.send("clear-range", name=k, **{"in": start_at, "out": end})

            for n in range(g_have, g_need):
                pt.send("paste-at", tracks=group, at=n * g_loop)

        after = pt.extents()
        longest = max(v[1] for v in after.values())
        print("\n  %d track(s) now %.1f s (%d loops, %d bars)"
              % (len(tracks), longest / float(rate), longest // loop,
                 (longest // loop) * bars))

        # Every track has to have grown. A paste that lands on only one track
        # still reports success -- which is exactly what happened with "Link
        # Track and Edit Selection" off: 25 of 26 tracks stayed at their
        # recorded length and the run looked clean.
        short = [k for k in tracks
                 if (after[k][1] - after[k][0]) < longest - loop // 2]
        if short:
            print("  WARNING - %d track(s) did not extend: %s"
                  % (len(short), ", ".join(short[:8])))


def cmd_name(args):
    """
    Name each Pro Tools track after the instrument that was captured onto it.

    The names come from the Fantom's own .SVQ song file. The exported SMF has
    none -- see svq.py for why, and for why the synth cannot be asked directly.
    """
    import svq
    from protools import Session

    title = os.path.splitext(os.path.basename(args.file))[0]
    folders = [args.svq_dir, os.path.dirname(os.path.abspath(args.file)),
               os.path.join(os.path.expanduser("~"), "fantom-recovery", "final")]
    if args.svq:
        candidates = [args.svq]
    else:
        candidates = svq.find_svq(title, folders)
    if not candidates:
        sys.exit("No .SVQ found for '%s'. Pass --svq FILE, or --svq-dir FOLDER."
                 % title)

    path = candidates[0]
    names = svq.part_names(path)
    print("\n  %s\n  %d part(s)" % (path, len(names)))
    if len(candidates) > 1:
        print("  (%d other revision(s) found; using the one with the most parts)"
              % (len(candidates) - 1))

    fixes = svq.load_overrides(args.overrides or
                               os.path.splitext(args.file)[0] + ".names")
    if fixes:
        print("  %d correction(s) applied from the overrides file" % len(fixes))
        names.update(fixes)

    # The patch name replaces the whole track name, number included.
    wanted = svq.track_names(names, prefix=args.number)

    with Session() as pt:
        have = set(pt.extents())
        # Tracks are matched by their leading number, not by position, so a
        # session with tracks missing or reordered still lines up.
        by_num = {}
        for t in have:
            m = re.match(r"^(\d+)", t)
            if m:
                by_num[int(m.group(1))] = t

        print()
        renamed = skipped = 0
        for num in sorted(wanted):
            current = by_num.get(num)
            if current is None:
                print("  part %-3d no track in the session" % num)
                skipped += 1
                continue
            if current == wanted[num]:
                print("  %-22s already named" % current[:22])
                continue
            if args.dry_run:
                print("  %-22s -> %s" % (current[:22], wanted[num]))
                continue
            pt.send("rename-track", name=current, to=wanted[num])
            print("  %-22s -> %s" % (current[:22], wanted[num]))
            renamed += 1

        if args.dry_run:
            print("\n  Dry run. Re-run without --dry-run to apply.")
            return

        # Read the names back: a rename that silently hit nothing still
        # returns ok, and the point of naming is that the labels are right.
        after = set(pt.extents())
        missing = [n for n in wanted.values() if n not in after]
        print("\n  %d renamed, %d skipped." % (renamed, skipped), end=" ")
        print("all names present" if not missing
              else "MISSING: %s" % missing)


def cmd_align(args):
    """Trim the capture lead off every stem so bar 1 lands on bar 1."""
    from audio import latest_takes
    from protools import Session, ProToolsError

    song = Song(args.file)
    order = list(song.parts.keys())
    midi_start = {}
    for i, key in enumerate(order, start=1):
        ev = song.parts[key]["events"]
        first = next((t for t, m in ev if m.type == "note_on" and m.velocity > 0), None)
        if first is not None:
            midi_start[i] = song.tick_to_sec(first)

    takes = latest_takes(args.session)
    print()
    print("  %-22s %9s %9s %9s" % ("TRACK", "AUDIO", "MIDI", "LEAD"))
    print("  " + "-" * 22 + " " + "-" * 9 + " " + "-" * 9 + " " + "-" * 9)

    leads = {}
    rate = 48000
    for name in sorted(takes, key=lambda s: (re.match(r"^(\d+)", s) or [0, "999"])[1]):
        m = re.match(r"^(\d+)", name)
        if not m:
            continue
        idx = int(m.group(1))
        t = takes[name]
        rate = t.rate
        if t.onset is None or idx not in midi_start:
            print("  %-22s %9s %9s %9s" % (name[:22], "-", "-", "skip"))
            continue
        lead = t.onset - midi_start[idx]
        leads[name] = lead
        print("  %-22s %8.3fs %8.3fs %8.3fs" % (name[:22], t.onset, midi_start[idx], lead))

    if not leads:
        sys.exit("\n  Could not measure a lead on any track.")

    # The dominant cluster, not the mean: a session usually holds takes from
    # several runs, and an average of two clusters belongs to neither.
    values = sorted(leads.values())
    best_i, best_n = 0, 0
    for i in range(len(values)):
        j = i
        while j < len(values) and values[j] - values[i] <= 0.15:
            j += 1
        if j - i > best_n:
            best_n, best_i = j - i, i
    cluster = values[best_i:best_i + best_n]
    lo, hi = min(cluster), max(cluster)

    # Trim by the SMALLEST lead in the cluster, never the median or mean.
    # The earliest-starting track defines where audio actually begins; cutting
    # by anything larger removes real material from every track below that
    # figure. `keep` then backs off a little further as insurance.
    lead = max(0.0, lo - args.keep)

    print()
    print("  cluster : %d of %d track(s) agree, %.3f-%.3f s" % (len(cluster), len(leads), lo, hi))
    print("  earliest: %.3f s  <- the trim is bounded by this" % lo)
    print("  trimming: %.3f s  (%.0f ms of headroom kept)" % (lead, args.keep * 1000))
    if hi - lo > 0.020:
        print("  note    : leads differ by %.0f ms, so tracks above the earliest keep"
              % ((hi - lo) * 1000))
        print("            that much silence. Better that than cutting audio.")
    outside = [n for n, l in leads.items() if not (lo <= l <= hi)]
    if outside:
        print("  leaving alone (different run): %s" % ", ".join(sorted(outside)[:6]))
    print()

    if args.dry_run:
        print("  Dry run - nothing changed.")
        print("  Session tempo should be %.2f BPM (set by hand; PTSL has no setter)."
              % song.tempo_bpm())
        return
    if lead <= 0.001:
        print("  Nothing to remove.")
        return

    samples = int(round(lead * rate))
    done = failed = 0
    with Session() as pt:
        for name in sorted(leads):
            if not (lo <= leads[name] <= hi):
                continue
            try:
                r = pt.trim_head(name, samples)
                print("  %-22s -%.3fs  verified" % (name[:22], r["removed"] / float(rate)))
                done += 1
            except ProToolsError as e:
                print("  %-22s FAILED: %s" % (name[:22], e))
                failed += 1
        pt.edit_mode("EMode_Slip")

    print()
    print("  %d track(s) aligned%s." % (done, ", %d failed" % failed if failed else ""))
    print("  Session tempo should be %.2f BPM (set by hand; PTSL has no setter)."
          % song.tempo_bpm())


def cmd_panic(args):
    """Silence the synth: MIDI Stop, all-sound-off, note-offs on every channel."""
    port, desc = open_output(args)
    with port:
        port.send(mido.Message("stop"))
        panic_all(port)
        for ch in range(16):
            for note in range(128):
                port.send(mido.Message("note_off", channel=ch, note=note, velocity=0))
    print("  Silenced %s" % desc)


def cmd_session(args):
    """What is actually on the Pro Tools timeline."""
    from protools import Session
    with Session() as pt:
        clips = pt.clips()
        edit = pt.edit_selection()
        print()
        print("  %-22s %-28s %12s %12s" % ("TRACK", "CLIP", "START", "END"))
        print("  " + "-" * 22 + " " + "-" * 28 + " " + "-" * 12 + " " + "-" * 12)
        for name in sorted(clips):
            if not clips[name]:
                print("  %-22s %-28s" % (name[:22], "(empty)"))
                continue
            for c in clips[name]:
                print("  %-22s %-28s %12d %12d" % (name[:22], c[0][:28], c[1], c[2]))
        print()
        print("  edit selection: %s" % (edit or "none"))
        print()


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
    print("  #  Part                     Ch   Notes   Bars   Loop   PC     CCs used")
    print("  -- ------------------------ ---- ------- ------ ------ ------ -----------")
    for i, (key, p) in enumerate(song.parts.items(), start=1):
        pcs = ",".join(str(x) for x in sorted(p["programs"])) or "-"
        ccs = ",".join(str(x) for x in sorted(p["ccs"])[:6]) or "-"
        if len(p["ccs"]) > 6:
            ccs += ",..."
        # Loop is what this part will actually be captured at, which is not
        # always the song's length -- see part_loop_bars.
        print("  %2d  %-24s %-4d %-7d %-6.1f %-6d %-6s %s" % (
            i, p["name"][:24], p["channel"] + 1, p["notes"],
            song.bars(p["max_tick"]), part_loop_bars(song, key), pcs, ccs))
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
    # The part's own number, not its position in the selection. Capturing
    # `--parts 17-` must record onto tracks 17..20; numbering the selection
    # from 1 would send them to tracks 01..04 and overwrite the wrong takes.
    numbers = parse_parts(args.parts, len(song.parts))
    region = parse_region(args.region)
    port, desc = open_output(args)
    print("  Port: %s" % desc)
    print("  Mode: one Pro Tools track per part\n")

    # Number tracks by their position in the SONG, not in the selection, so
    # "--parts 12" re-records onto "12 Track 12" rather than creating a new
    # "01 Track 12" beside it.
    song_order = dict((k, i) for i, k in enumerate(song.parts.keys(), start=1))

    enable_hires_timer()
    made = 0
    # One persistent PTSL connection for the whole pass. Spawning node per
    # command cost 150-300ms of *variable* startup, which landed between
    # "recording started" and "MIDI started" and left every take at a slightly
    # different offset -- the stems drifted out of sync with each other.
    pt = PTools()
    if not pt.ready:
        print("  WARNING: could not open a persistent Pro Tools connection;")
        print("           falling back to per-command mode (takes may drift).")
    try:
        with port:
            panic_all(port)
            for pos, (idx, key) in enumerate(zip(numbers, keys), start=1):
                part = song.parts[key]
                # Each part loops at its own length unless one was set by hand.
                part_bars = args.bars or part_loop_bars(song, key)
                events, cues, total, loop_bars, skipped = build_schedule(
                    song, [key], args.loops, 0, args.lead, part_bars,
                    args.send_programs, region=region, include_empty=args.include_empty)
                if not cues:
                    print("  %2d/%d  %-22s (no notes in region, skipped)"
                          % (pos, len(keys), part["name"]))
                    continue
                if args.clock:
                    events = sorted(events + clock_events(
                        song, total, start_stop=args.clock_start, bpm=args.clock_bpm),
                        key=lambda e: e[0])

                name = re.sub(r"[^A-Za-z0-9 _-]", "", part["name"]).strip()[:24]
                track = "%02d %s" % (idx, name or ("Part %d" % idx))

                # Announce the part before rolling, not after. A part takes the
                # better part of a minute and printed nothing until it was over,
                # which left anything watching this output with no way to show
                # what was happening -- or which file to meter.
                # Both numbers, because they are different things: position in
                # this pass drives the progress bar, and [part] says which row
                # of the song it is. With --parts 14- they diverge, and a
                # reader that assumed one number put every level and highlight
                # on the wrong track.
                print("  >> %2d/%d  [%d] %-22s ch%-3d recording -> %s"
                      % (pos, len(keys), idx, part["name"],
                         part["channel"] + 1, track),
                      flush=True)

                pt("ensure-track", name=track)
                pt("disarm-all")
                pt("arm", name=track)
                pt("locate", samples=0)
                # 'record' returns only once the transport is genuinely
                # recording, so the pre-roll here only needs to cover converter
                # latency -- not several seconds of dead air at the top.
                pt("record")
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
                    pt("stop")
                    for m in panic_messages(part["channel"]):
                        port.send(m)

                if failed is not None:
                    print("  %2d/%d  %-22s FAILED: %s"
                          % (pos, len(keys), part["name"], failed))
                    print("         Pro Tools stopped. Aborting the pass.")
                    break
                made += 1
                print("  %2d/%d  [%d] %-22s ch%-3d %s  -> %s"
                      % (pos, len(keys), idx, part["name"], part["channel"] + 1,
                         fmt_time(total), track), flush=True)
                time.sleep(0.35)
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        pt("stop")
    finally:
        release_hires_timer()
        pt("disarm-all")
        pt.close()

    print("\n  Done. %d track(s) recorded, each starting at timeline zero." % made)

    if made and not args.no_trim:
        trim_after_capture(args)
    if made and not args.no_extend:
        extend_after_capture(args)
    if made and not args.no_name:
        name_after_capture(args)


def trim_after_capture(args):
    """
    Trim every recorded stem to its own first attack, for real.

    Runs the same path as `fantom_stem.py tab`: Pro Tools' own Tab to
    Transient and Separate Clip, driven through the MCP server. No dry run and
    no confirmation, since the pass has just finished and nobody is watching.
    """
    session = args.session or session_path_from_protools()
    if not session:
        print("\n  Head trim skipped: no Pro Tools session folder known.")
        print("  Pass --session <folder> to trim automatically after a capture.")
        return

    print("\n  Trimming heads to the first attack...\n")

    ns = argparse.Namespace(
        session=session,
        max_head=0,              # no ceiling: trim to the transient, always
        tracks=None,
        force=False,             # stop rather than trim blind on a warning
        dry_run=False,
        yes=True,
    )
    try:
        cmd_tab(ns)
        return True
    except SystemExit as e:
        # cmd_tab exits when nothing is measurable; that must not kill the pass.
        if e.code:
            print("  Head trim: %s" % e.code)
        return not e.code
    except Exception as e:
        print("  Head trim failed: %s" % e)
        return False


def extend_after_capture(args):
    """Fill the session out to a usable length once the heads are trimmed."""
    print("\n  Repeating the second loop to fill out the song...")
    ns = argparse.Namespace(
        file=args.file,
        minutes=args.minutes,
        bars=args.bars,
        dry_run=False,
    )
    try:
        cmd_extend(ns)
    except SystemExit as e:
        # Nothing to extend must not look like a failed capture.
        if e.code:
            print("  Extend: %s" % e.code)
    except Exception as e:
        print("  Extend failed: %s" % e)


def name_after_capture(args):
    """Name the tracks after the Fantom patches once the audio is in place."""
    print("\n  Naming tracks from the Fantom song file...")
    ns = argparse.Namespace(
        file=args.file,
        svq=None,
        svq_dir=args.svq_dir,
        number=False,
        overrides=None,          # SONG.names beside the MIDI, if it exists
        dry_run=False,
    )
    try:
        cmd_name(ns)
    except SystemExit as e:
        # No .SVQ for this song is a normal state, not a failed capture.
        if e.code:
            print("  Naming: %s" % e.code)
    except Exception as e:
        print("  Naming failed: %s" % e)


def session_path_from_protools():
    """The open session's folder, asked of Pro Tools itself."""
    try:
        from protools import Session
        pt = Session()
        try:
            folder = (pt.send("session-path") or {}).get("folder") or ""
        finally:
            pt.close()
        if folder and os.path.isdir(folder):
            return folder
    except Exception:
        pass
    return None


def cmd_run(args):
    song = Song(args.file)
    keys = select_parts(song, args.parts)
    if args.per_track:
        # Per-track takes each start at timeline zero, so a lead bar and a long
        # pre-roll are pure silence at the head of every stem. Unless the user
        # asked for them explicitly, drop them.
        if "--lead" not in sys.argv:
            args.lead = 0
        if "--pt-preroll" not in sys.argv:
            args.pt_preroll = 0.3
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

    mk = sub.add_parser("markers", help="list or remove session memory locations")
    mk.add_argument("--clear", action="store_true", help="remove them")
    mk.add_argument("--all", action="store_true",
                    help="include markers this tool did not create")
    mk.add_argument("--yes", action="store_true", help="skip the confirmation")
    mk.set_defaults(func=cmd_markers)

    v = sub.add_parser("verify", help="measure the recorded stems")
    v.add_argument("session", nargs="?", default=os.environ.get("PT_SESSION", "."))
    v.add_argument("--detail", action="store_true", help="full report per take")
    v.set_defaults(func=cmd_verify)

    nm = sub.add_parser("name", help="name Pro Tools tracks after the Fantom patches")
    nm.add_argument("file", help="the MIDI file that was captured")
    nm.add_argument("--svq", metavar="FILE", help="use this .SVQ instead of searching")
    nm.add_argument("--svq-dir", metavar="FOLDER", help="extra folder to search")
    nm.add_argument("--number", action="store_true",
                    help="keep the part number in front of the patch name")
    nm.add_argument("--overrides", metavar="FILE",
                    help="name corrections (default: SONG.names beside the MIDI)")
    nm.add_argument("--dry-run", action="store_true", help="show the plan only")
    nm.set_defaults(func=cmd_name)

    xt = sub.add_parser("extend", help="repeat the second loop to fill out the song")
    xt.add_argument("file", help="the MIDI file that was captured")
    xt.add_argument("--minutes", type=float, default=3.5,
                    help="how long the result should be (default 3.5)")
    xt.add_argument("--bars", type=int, default=None,
                    help="bars per loop (default: the same length the capture "
                         "looped, taken from the song)")
    xt.add_argument("--dry-run", action="store_true", help="show the plan only")
    xt.set_defaults(func=cmd_extend)

    tb = sub.add_parser("tab", help="tab to transient, split, delete left, shift left")
    tb.add_argument("session", nargs="?",
                    help="Pro Tools session folder (default: ask Pro Tools)")
    tb.add_argument("--max-head", type=int, default=0,
                    help="ceiling in samples; 0 (the default) means no ceiling, "
                         "so every track is trimmed to its transient wherever "
                         "that falls")
    tb.add_argument("--tracks", metavar="NAME", nargs="+",
                    help="only these track names")
    tb.add_argument("--force", action="store_true",
                    help="trim even though preflight raised warnings")
    tb.add_argument("--dry-run", action="store_true", help="show the plan only")
    tb.add_argument("--yes", action="store_true", help="apply the trims")
    tb.set_defaults(func=cmd_tab)

    al = sub.add_parser("align", help="trim the capture lead so bar 1 is bar 1")
    al.add_argument("file", help="the SMF that was captured")
    al.add_argument("session", nargs="?", default=os.environ.get("PT_SESSION", "."))
    al.add_argument("--dry-run", action="store_true", help="measure, change nothing")
    al.add_argument("--keep", type=float, default=0.020,
                    help="seconds of headroom left before the earliest onset "
                         "(default 0.020 - insurance against cutting audio)")
    al.set_defaults(func=cmd_align)

    se = sub.add_parser("session", help="show what is on the Pro Tools timeline")
    se.set_defaults(func=cmd_session)

    pa = sub.add_parser("panic", help="silence the synth immediately")
    pa.add_argument("--port")
    pa.add_argument("--usb", action="store_true")
    pa.set_defaults(func=cmd_panic)

    i = sub.add_parser("inspect", help="analyse an SMF")
    i.add_argument("file")
    i.set_defaults(func=cmd_inspect)

    def common(p):
        p.add_argument("file")
        p.add_argument("--parts",
                   help="which parts to capture: 7, 17- (17 to the end), 5-8, "
                        "-4, or a mixture like 1,3,9-12 (default all)")
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
    r.add_argument("--no-trim", action="store_true",
                   help="leave the capture lead at the head of every take. By default a "
                        "finished pass trims each stem to its own first attack and packs "
                        "it to timeline zero")
    r.add_argument("--no-name", action="store_true",
                   help="leave the Pro Tools track names alone. By default a "
                        "finished pass names each track after the Fantom patch "
                        "it captured, read from the song's .SVQ file")
    r.add_argument("--svq-dir", metavar="FOLDER",
                   help="extra folder to search for the .SVQ song file")
    r.add_argument("--no-extend", action="store_true",
                   help="leave the session at its recorded length. By default a "
                        "finished pass repeats the second loop until the song is "
                        "--minutes long")
    r.add_argument("--minutes", type=float, default=3.5,
                   help="how long to make the song after capture (default 3.5)")
    r.add_argument("--session", metavar="FOLDER",
                   help="Pro Tools session folder, for the post-capture trim. Asked of "
                        "Pro Tools directly when not given")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

