# Pro Tools Fantom-G Auto Recorder

Unattended per-part stem capture from a Roland Fantom-G into Pro Tools.

Load a song, press one key, walk away. Every part is recorded to its own
track, trimmed to its first note, repeated out to a usable song length, and
named after the patch that played it.

It replaces the manual loop: mute everything but one part, arm a track,
record, wait, stop, trim, rename, repeat — for thirty parts.

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
              one track per part, trimmed, extended, named

Two things make it unattended rather than merely scripted. Every destructive
edit is **verified against the session** — Pro Tools reports success for
commands that acted on the wrong track, or on nothing at all. And each
finished take is measured, so a silent or clipped part is reported rather
than discovered days later.

## Quick start

Double-click **`A_RunFantomCapture.bat`**, or:

    .\Fantom-Capture.ps1

`F2` load a song, `R` record, `S` stop if anything goes wrong.

From the command line:

    python fantom_stem.py plan song.mid              # preview, sends nothing
    python fantom_stem.py run  song.mid --usb --per-track --protools

## What a capture does

A finished `--per-track` pass runs three steps by itself. Each can be
switched off, and each is also a subcommand you can run on its own.

**1. Trim** — every stem is pulled back to its own first attack and packed to
timeline zero. `--no-trim` leaves the lead alone.

**2. Extend** — the settled loop is repeated until the session reaches
`--minutes` (default 3.5). `--no-extend` leaves the recorded length.

**3. Name** — each Pro Tools track is renamed after the Fantom patch that
played it. `--no-name` leaves the numbered names.

    python fantom_stem.py tab                     # trim, on demand
    python fantom_stem.py extend song.mid         # repeat to length
    python fantom_stem.py name   song.mid         # name from the .SVQ
    python fantom_stem.py verify  <session>       # measure every take
    python fantom_stem.py session <session>       # what is on the timeline

`tab` needs no session path — it asks Pro Tools which session is open.

## The trim asks Pro Tools, it does not guess

PTSL exposes no Tab to Transient, no Separate Clip and no nudge. An earlier
version measured the recorded audio and tried to predict where Pro Tools
would land. Tuned until it matched one track's hand edit, it then cut into
the attack of the other nineteen — measured afterwards, every one had note
material in the 40 ms before the cut.

The trim now drives Pro Tools' own menu commands through the
[protools-mcp-server](https://github.com/skrul/protools-mcp-server), which
reaches what PTSL cannot. Against a hand edit it reproduces the cut to 1 ms.

Two things this makes possible, and one trap it exposed:

- Tracks are trimmed **one at a time**, each checked against the session
  before the next. A bad run damages one track, not thirty.
- There is **no head ceiling** by default. Whether a late transient is a soft
  attack or dead air is a judgement about the music, not one the tool makes.
- **A focused floating Pro Tools window swallows the Tab keystroke.** Tab
  silently does nothing and the reported transient is wherever the insertion
  already sat, which produced cuts ranging from 21 ms to 0.99 s on one
  recording — every one looking like a clean trim. Preflight warns about it.
  Close floating windows before an unattended pass.

## Loop lengths, per part

A song does not have to use one loop length. `BLASTINGROCK` has seventeen
8-bar parts and fifteen 4-bar ones. Forcing a single length either truncates
the long parts or gives the short ones half an iteration of silence, so each
part is measured on its own:

- The **base** is the length most parts share.
- A **longer** part rounds up to a whole multiple — 16 over a base of 8.
- A **shorter** part keeps its length if it divides the base evenly and is at
  least half of it, so 4 into 8 is a four-bar loop that repeats twice.
- Anything else rounds up. A part whose notes stop in bar 5 of 8 is a part
  that ends early, not a 5-bar loop, and looping it at 5 would drag it out of
  time. A part with one hit near the start is not a one-bar loop.

Loop length is measured from the last note **start**, not the last event: a
held release crossing the final bar line is not another bar of music.

The console shows the result per part in the **BARS** column. `B` overrides
it for the whole song when a song disagrees.

`extend` groups tracks by their loop length and repeats each group at its own
spacing, all measured in units of the shortest loop so every track ends on
the same sample.

## Track names come from the .SVQ

The exported SMF has no track names, no program changes and no bank selects —
the `Track 1`…`Track 20` you see are generated. The Fantom's own song file
keeps the patch name for every part, and `name` reads it:

    python fantom_stem.py name song.mid --dry-run

`.SVQ` files are matched on alphanumerics only, since the Fantom writes
`%tOGEE WIZARDt%.SVQ` for what exports as `TOGEEWIZARD.mid`. Where several
revisions exist the one with the most parts wins. Duplicated patches are
numbered — a lone bass stays `Bass`, two guitars become `ElectricGuitar1` and
`ElectricGuitar2`.

Files recovered from a damaged card sometimes have a byte missing in a name.
Fix those once in a `SONG.names` file beside the MIDI, one per line:

    1  = Chicken Bass
    16 = Classic HipHop

## Choosing parts

`--parts` takes ranges, which matters when re-recording the tail of a song
after fixing a few sounds:

    --parts 7           just part 7
    --parts 17-         part 17 to the end
    --parts 5-8         parts 5 to 8
    --parts -4          parts 1 to 4
    --parts 1,3,9-12    any mixture

Parts record onto their **own** numbered track: `--parts 17-` writes to
tracks 17 upward, not 1 upward. In the console, `P` sets this and the skipped
parts grey out in the table.

## Two capture modes

**Per-track** (`--per-track`) — one Pro Tools track per part, each starting
at timeline zero, created and named automatically. This is what the console
uses.

**Single pass** — the whole capture in one continuous recording, split
afterwards at the bar positions in the generated cue sheet. Every stem then
shares a single latency offset: nudge the file once so the first part lines
up and everything else stays correct.

## Console keys

    [F2] song   [F3] region  [F5] device  [F6] arm    [F7] verify
    [A]  trim   [N] name     [P] parts    [B] bars    [L] loops
    [C]  clock  [R] record   [S] stop     [F10] quit
    [F]  follow  arrows/PgUp/PgDn/Home/End scroll parts

`S` works during a capture and when idle. `Esc`, `Q`, `F10`, `Space` and
`Ctrl+C` stop a running pass too — mid-capture there was once no way out at
all.

## Stopping

    .\Stop-Capture.ps1        # or press S in the console

Safe at any time, including when nothing is wrong. A crashed capture leaves
Pro Tools rolling and record-armed, orphaned processes, and a synth still
sounding. This clears all three, in that order — the capture process first so
it cannot send more notes, then the transport, then a MIDI panic on every
channel. Panicking before killing the sender does nothing.

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

The trim additionally needs [protools-mcp-server](https://github.com/skrul/protools-mcp-server)
built (`npm install && npm run build`), with `PT_MCP_SERVER` pointing at its
`dist/index.js` if it is not at the default path. It must run with
`ALLOW_WRITES=all`; it is read-only otherwise and every edit is refused.

## Reaching the synth

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

**A normal MIDI port** (`--port "name"`) — a USB MIDI interface into the
Fantom's 5-pin DIN jacks, or any OS-visible MIDI output.

    python usb_probe.py     dump the USB descriptors
    python usb_diag.py      receive test and 16-channel sweep

Note that **nothing arrives back over the Fantom's own USB port** — not note
data, not SysEx replies, on either IN endpoint and either alternate setting.
Sending works perfectly. A 5-pin interface (a Roland UM-ONE was used here)
does receive, if you need the return path.

## Options for plan / run

    --parts SPEC        Which parts: 7, 17-, 5-8, -4, 1,3,9-12 (default all)
    --loops N           Loop iterations per part (default 3)
    --gap BARS          Silence between parts, for tails (default 2)
    --lead BARS         Silence before the first part (default 1)
    --bars N            Loop length in bars (default: per part, from the song)
    --minutes N         Length to extend to after capture (default 3.5)
    --no-trim           Leave the capture lead on every take
    --no-extend         Leave the session at its recorded length
    --no-name           Leave the numbered track names
    --svq-dir FOLDER    Extra folder to search for the .SVQ
    --send-programs     Send program changes (default: OFF)
    --clock             Emit MIDI clock, for arpeggiator and RPS parts
    --yes               Skip the confirmation prompt

## Design notes

**Why three loop iterations.** The cue sheet's KEEP column points at the
*final* iteration. On the first pass reverb tails and delay feedback have
not reached steady state, so the top of the loop is dry in a way it never is
when you hear it cycle. By the third, the previous cycle's tail is present
and the stem sounds like what was in the room. `extend` repeats that settled
loop for the same reason. Increase `--gap` for long reverbs.

**Why program changes are off.** Your Live Set already has the right patch
on every part; sending program changes can only move away from that. Turn it
on only when the SMF is the sole source of truth for patches.

**One persistent PTSL connection per pass.** Spawning a process per command
cost 150–300 ms of *variable* startup, landing between "recording started"
and "MIDI started". Every take was offset differently and the stems drifted
apart. Holding the connection open brought take-to-take timing to ~13 ms.

**Failure is judged by exit code.** Python writes tracebacks to stderr, which
was not redirected — a pass that died on its first line looked exactly like
one still running, while Pro Tools kept recording. A failed pass now prints
the error and stops the transport.

## Known limitations

**The Fantom will not answer questions.** It replies to a universal Identity
Request over DIN — Roland, family `27 02`, device `0x10` — but ignores Roland
Data Requests on every model ID and address tried. Patch names therefore come
from the `.SVQ` file rather than from the instrument.

**Songs cannot be switched remotely.** Song Select is ignored, and Studio Set
switching only helps if you keep a saved Studio Set per song. Loading the next
song is a keypress on the Fantom.

**Empty tracks are not exported.** If Fantom track 13 is empty, MIDI part 14
is Fantom track 15 and everything after the gap shifts by one. Part numbering
follows the MIDI, so check the numbers against the instrument.

**Session tempo must be set by hand.** PTSL has no command for it.

**Arpeggiator and RPS parts.** If a part is driven by the arpeggiator, chord
memory or RPS, the SMF may hold only the trigger chord rather than the
resulting pattern. `inspect` flags parts with suspiciously few notes. Note
that All Notes Off does not stop an arpeggiator — it keeps generating, which
sounds like the part never ends. `--clock` runs the arpeggiator in time with
the notes sent.

## Contributing

Two files are worth reading before changing anything:

- **[FEATURES.md](FEATURES.md)** — what already exists, and where. Most of
  what looks missing is behind a subcommand or a single key.
- **[AGENTS.md](AGENTS.md)** — how to work here without breaking a session,
  and the traps that have already cost time.

## License

MIT — see [LICENSE](LICENSE).
