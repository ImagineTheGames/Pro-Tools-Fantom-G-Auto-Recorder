# Pro Tools Fantom-G Auto Recorder

Unattended per-part stem capture from a Roland Fantom-G into Pro Tools.

Load a song, press one key, walk away. Every part is recorded to its own
track, each starting at its first note.

It replaces the manual loop: mute everything but one part, arm a track,
record, wait, stop, rename, repeat — for sixteen parts.

---

## How it works

The synth's own sequencer is not used. The song is exported as a Standard
MIDI File, and this tool plays **one part at a time** out to the Fantom. The
synth still produces all the audio through its own engine, patches and
effects; the only thing that changes is where the note data comes from.
Nothing is muted — the other parts' notes are simply not sent.

    song.mid ──► fantom_stem.py ──► raw USB MIDI ──► Fantom-G
                       │                                │
                       │ PTSL over gRPC                 │ analogue outs
                       ▼                                ▼
                  Pro Tools  ◄───────────────────── audio interface
                       │
                       ▼
              one track per part, trimmed to its first note

Two things make it unattended rather than merely scripted. The transport is
driven over PTSL and **verified** — Pro Tools reports success for commands
that acted on the wrong track, so every destructive edit checks the session
EDL before and after. And each finished take is measured, so a silent or
clipped part is reported rather than discovered days later.

## Quick start

    .\Fantom-Capture.ps1

`F2` load a song, `F9` capture, `S` stop if anything goes wrong.

Or from the command line:

    python fantom_stem.py plan song.mid              # preview, sends nothing
    python fantom_stem.py run  song.mid --usb --per-track --protools

## Two capture modes

**Per-track** (`--per-track`) — one Pro Tools track per part, each starting
at timeline zero, created and named automatically. This is the mode the
console uses.

**Single pass** — the whole capture in one continuous recording, split
afterwards at the bar positions in the generated cue sheet. Every stem then
shares a single latency offset: nudge the file once so the first part lines
up and everything else stays correct.

## After the capture

**The trim runs by itself.** A finished `--per-track` pass pulls every stem
it recorded back to its own first attack and packs it to timeline zero — no
dry run, no prompt. `--no-trim` leaves the lead in place.

    python fantom_stem.py verify   <session>   # measure every take
    python fantom_stem.py session  <session>   # what is on the timeline
    python fantom_stem.py tab      <session> --grid song.mid --dry-run

`tab` is the same trim on demand: tab to transient, split, delete the left,
shift left. `--grid` keeps parts that do not begin on beat 1 where they
belong. The console's `A` key runs it with a preview first.

`align` does the same job by trimming the shared capture lead, using the
**smallest** lead in the group so it can never cut into the earliest part.

## Console keys

    [F2] song   [F3] region  [F5] device  [F6] arm    [F7] verify
    [A]  trim   [L] loops    [C] clock    [F9] capture
    [S]  stop   [F10] quit   [F] follow   arrows/PgUp/PgDn scroll parts

## Stopping

    .\Stop-Capture.ps1        # or press S in the console

Safe at any time, including when nothing is wrong. A crashed capture leaves
Pro Tools rolling and record-armed, orphaned processes, and a synth still
sounding. This clears all three, in that order — Pro Tools first because it
is the one that damages a session, the MIDI panic last because the running
capture owns the Fantom's USB endpoint until it is gone.

`-KeepProcesses` stops the transport but leaves a running take alone.
`-NoPanic` leaves the synth sounding. If the Fantom still makes noise
afterwards, press STOP on the instrument — once USB is unavailable, nothing
on the computer can reach it.

## Requirements

Windows, Python 3.10+, Node 18+, Pro Tools with PTSL enabled.

    python -m pip install mido python-rtmidi pyusb libusb-package
    npm install

`ptools.js` needs Avid's `PTSL.proto` from the PTSL SDK. It defaults to a
local SDK path; override with `PTSL_PROTO_PATH`.

## Reaching the synth

**A normal MIDI port** (`--port "name"`) — a USB MIDI interface into the
Fantom's 5-pin DIN jacks, or any OS-visible MIDI output.

**Raw USB** (`--usb`) — talks to the Fantom directly over libusb, bypassing
Windows' MIDI stack. This exists because Roland's last Fantom-G driver was
for Windows 8.1 and its kernel driver will not load on Windows 11.

The Fantom-G presents a vendor-specific interface (class `FF`) so Windows
will not auto-bind, but interface 2 is subclass `03` — MIDI Streaming —
with a standard bulk endpoint pair:

    INTERFACE 2  class=ff sub=03 proto=00
        EP 0x03  OUT  BULK  512     <- MIDI to the Fantom
        EP 0x82  IN   BULK  512     <- MIDI from the Fantom

Bind WinUSB to the device with Zadig. No kernel driver, no signature or
Memory Integrity changes — WinUSB is Microsoft's own signed driver, and it
is reversible from Device Manager.

    python usb_probe.py     dump the USB descriptors
    python usb_diag.py      receive test and 16-channel sweep

## Options for plan / run

    --parts 1,3,7       Only these parts (default: all)
    --loops N           Loop iterations per part (default 3)
    --gap BARS          Silence between parts, for tails (default 2)
    --lead BARS         Silence before the first part (default 1)
    --bars N            Loop length in bars (default: auto from file length)
    --send-programs     Send program changes (default: OFF)
    --clock             Emit MIDI clock, for arpeggiator and RPS parts
    --yes               Skip the confirmation prompt

## Design notes

**Why three loop iterations.** The cue sheet's KEEP column points at the
*final* iteration. On the first pass reverb tails and delay feedback have
not reached steady state, so the top of the loop is dry in a way it never is
when you hear it cycle. By the third, the previous cycle's tail is present
and the stem sounds like what was in the room. The gap between parts exists
for the same reason — increase `--gap` for long reverbs.

**Why program changes are off.** Your Live Set already has the right patch
on every part; sending program changes can only move away from that. Turn it
on only when the SMF is the sole source of truth for patches.

**Cue sheet.** `<song>_cues.csv` lands next to the SMF with `start_sec`,
`start_bar`, `keep_sec`, `keep_bar` and `end_sec` per part. Match the
session tempo to the SMF and the bar numbers fall on grid lines.

## Known limitation: arpeggiator and RPS parts

If a part is driven by the arpeggiator, chord memory or RPS, the SMF may
hold only the trigger chord rather than the resulting pattern. `inspect`
flags parts with suspiciously few notes. Capture those the old way, or
re-record them to a normal sequencer track on the synth first. `--clock`
exists for this case: it runs the arpeggiator in time with the notes sent.

## Contributing

Two files are worth reading before changing anything:

- **[FEATURES.md](FEATURES.md)** — what already exists, and where. Most of
  what looks missing is behind a subcommand or a single key.
- **[AGENTS.md](AGENTS.md)** — how to work here without breaking a session,
  and the traps that have already cost time.

## License

MIT — see [LICENSE](LICENSE).
