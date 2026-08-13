# fantom-stem

Per-part stem capture for hardware synths. Replaces the manual
"mute everything but one track, record, repeat" workflow.

## How it works

The synth's own sequencer is not used. Instead the song is exported as a
Standard MIDI File, and this tool plays **one part at a time** out to the synth
over MIDI. The synth still generates all the audio through its own engine,
patches and effects — the only thing that changes is where the note data comes
from and how isolation is achieved. Nothing is muted; the other parts' notes
are simply not sent.

Because each part is short, the whole capture fits in **one continuous DAW
record pass**. Arm one stereo track, hit record once, run the tool, stop. Then
split the resulting file at the bar positions in the generated cue sheet.

## Setup

Python and the two libraries are already installed at:

    C:\Users\Rei\AppData\Local\Programs\Python\Python312\python.exe

If you ever need to reinstall the libraries:

    python -m pip install mido python-rtmidi

## Two ways to reach the synth

**A normal MIDI port** (`--port "name"`) — via a USB MIDI interface into the
Fantom's 5-pin DIN jacks, or any other OS-visible MIDI output.

**Raw USB** (`--usb`) — talks to the Fantom directly over libusb, bypassing
Windows' MIDI stack entirely. This exists because Roland's last Fantom-G driver
was for Windows 8.1 and its kernel driver won't load on Windows 11.

The Fantom-G presents a vendor-specific USB interface (class `FF`) so Windows
won't auto-bind to it, but underneath, interface 2 is subclass `03` — MIDI
Streaming — with a standard bulk endpoint pair:

    INTERFACE 2  class=ff sub=03 proto=00
        EP 0x03  OUT  BULK  512     <- MIDI to the Fantom
        EP 0x82  IN   BULK  512     <- MIDI from the Fantom

Interfaces 0 and 1 are the isochronous audio endpoints, unused here.

To use the raw USB path, WinUSB must be bound to the device (via Zadig).
No kernel driver, no signature or Memory Integrity changes — WinUSB is
Microsoft's own signed driver. Reversible from Device Manager.

    python usb_probe.py     dump the USB descriptors
    python usb_diag.py      full diagnostic: receive test + 16-channel sweep

## Commands

    python fantom_stem.py ports
        List MIDI input/output ports.

    python fantom_stem.py test --port "Fantom" --channel 5
        Play a short four-note run on one channel. Use this to confirm the
        cable and the channel-to-part mapping before a real pass.

    python fantom_stem.py inspect song.mid
        Show what's on each part: channel, note count, length in bars,
        program changes, CCs used. Flags parts that look like arpeggiator
        or RPS triggers.

    python fantom_stem.py plan song.mid
        Preview the capture layout and write the cue sheet. Sends no MIDI.

    python fantom_stem.py run song.mid --port "Fantom"
        Perform the capture pass.

## Stopping a capture

    .\Stop-Capture.ps1

Safe at any time, including when nothing is wrong. A crashed or abandoned
capture leaves three things behind, and this clears all of them in the order
that matters:

1. **Pro Tools still rolling and record-armed.** It will record over the next
   take, or fill a drive. Stopped first, then every track is disarmed.
2. **Orphaned processes** — `fantom_stem.py run` and its `ptools.js serve`
   client. Matched on command line, never on image name: killing every
   `node.exe` would take out unrelated tools.
3. **The Fantom still sounding**, because the note-offs never got sent.
   Silenced last, deliberately — the running capture owns the USB endpoint,
   so `panic.py` cannot open it until that process is gone.

`-KeepProcesses` stops the transport but leaves a running take alone.
`-NoPanic` leaves the synth sounding.

If the Fantom is still making noise afterwards, press STOP on the
instrument or turn it down — nothing on the computer can reach it once USB
is unavailable.

## Options for plan / run

    --parts 1,3,7       Only these parts (default: all)
    --loops N           Loop iterations per part (default 3)
    --gap BARS          Silence between parts, for tails (default 2)
    --lead BARS         Silence before the first part (default 1)
    --bars N            Loop length in bars (default: auto from file length)
    --send-programs     Send program changes (default: OFF)
    --yes               Skip the "press Enter" prompt (run only)

## Why three loop iterations

The **KEEP** column in the cue sheet points at the start of the *final*
iteration. Use that one.

On the first pass through a loop, reverb tails and delay feedback haven't
reached steady state — the top of the loop is dry in a way it never is when
you're actually listening to it cycle. By the third iteration the tail from
the previous cycle is present, and the stem sounds like what you heard in the
room.

The gap between parts exists for the same reason: it lets each part's tail
decay fully so it doesn't bleed into the top of the next one. Increase `--gap`
if you're using long reverbs.

## Why program changes are off by default

Your Live Set / Studio Set already has the right patch on every part. Sending
program changes can only move things away from that. Leave it off unless the
SMF is the sole source of truth for patch assignment.

## Latency alignment

Because the whole capture is one continuous recording, every stem shares a
single latency offset. Nudge the recorded file once so the first part lines up,
and every other part stays correctly aligned to it automatically. There is no
per-stem alignment to do.

## Cue sheet

`<song>_cues.csv` is written next to the SMF, with `start_sec`, `start_bar`,
`keep_sec`, `keep_bar` and `end_sec` for every part. Set your DAW session tempo
to match the SMF and the bar numbers land exactly on grid lines.

## Transport and clock

    python fantom_stem.py transport start --usb
    python fantom_stem.py transport stop --usb
    python fantom_stem.py transport continue --usb

Sends MIDI real-time messages (Start `FA`, Stop `FC`, Continue `FB`). Add
`--songpos N` to locate to a beat before starting, or `--mmc` to use MIDI
Machine Control SysEx instead if the synth ignores real-time messages.

**These only drive the synth's sequencer if its Sync setting has it following
external MIDI.** If nothing happens, that setting is the first thing to check.

### --clock during a capture pass

    python fantom_stem.py run song.mid --usb --clock

Emits MIDI clock at the standard 24 PPQN for the whole pass, bookended with
Start and Stop. This exists so the synth's **arpeggiator and RPS run in time
with the notes being sent** — see the limitation below, which `--clock` is the
answer to.

## Known limitation: arpeggiator and RPS parts

If a part is driven by the synth's arpeggiator, chord memory, or RPS, the SMF
may contain only the trigger chord rather than the resulting pattern. `inspect`
flags parts with suspiciously few notes. Those need to be captured the old way,
or re-recorded to a normal sequencer track on the synth first.
