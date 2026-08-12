# Pro Tools Fantom G Auto Recorder

Records every part of a Roland Fantom-G song into Pro Tools as an isolated
audio stem, unattended — replacing the manual "mute everything but one track,
hit record, repeat twenty times" routine.

It plays **one part at a time** to the synth over MIDI and drives Pro Tools'
transport in step, so a twenty-part song becomes twenty named, aligned tracks
without anyone in the room.

Two things make it work on hardware that Windows officially abandoned:

- **Raw USB MIDI.** Roland's last Fantom-G driver was for Windows 8.1. This
  bypasses it entirely, talking to the synth's bulk endpoint through WinUSB.
  No vendor driver, no Secure Boot changes, no unsigned kernel drivers.
- **PTSL.** Pro Tools' scripting API creates the tracks, arms them, rolls
  record and stops — synchronised with MIDI playback rather than guessed at.

Measured timing on the reference rig: **0.018 ms mean scheduling latency**,
0.8 ms worst case. Tighter than the MIDI wire itself.

---

## How it works

```
  Standard MIDI File
          │
          │  one part's notes at a time
          ▼
   [ this tool ] ──raw USB bulk──►  Fantom-G sound engine
          │                                  │
          │  PTSL over gRPC                  │  analogue outs
          ▼                                  ▼
     Pro Tools  ◄───────────────────  audio interface
```

The synth's own sequencer is never used. Isolation happens by simply **not
sending** the other parts' notes — nothing is muted, and your Live Set stays
exactly as you left it. No program changes are sent either, so whatever
patches you have loaded are what gets recorded.

---

## Requirements

| | |
|---|---|
| Synth | Roland Fantom-G6 / G7 / G8 |
| DAW | Pro Tools 2024.10 or later (PTSL) |
| OS | Windows 10 / 11 |
| Python | 3.10+ with `mido`, `python-rtmidi`, `pyusb`, `libusb-package` |
| Node | 18+ (for the PTSL client) |
| SDK | Avid PTSL SDK — **you must obtain this yourself**, see below |
| Driver | WinUSB bound to the Fantom via [Zadig](https://zadig.akeo.ie) |

Audio is recorded through whatever interface your synth's outputs are cabled
to. The tool never touches audio routing — only MIDI and the transport.

---

## Setup

**1. Python dependencies**

```
pip install mido python-rtmidi pyusb libusb-package numpy
```

`numpy` is only needed for the spectral analysis in `tools/`.

**2. Bind WinUSB to the Fantom**

The Fantom-G presents a vendor-specific USB interface (class `FF`), so Windows
won't attach a driver to it. Use [Zadig](https://zadig.akeo.ie):

- Options → **List All Devices**
- Select **Fantom G** — confirm the USB ID reads `0582 00DE`
- Choose **WinUSB** and click Replace Driver

> Check that USB ID carefully. Replacing the driver on the wrong device will
> break it. Reversible from Device Manager → Uninstall device.

Underneath the vendor class code the synth speaks ordinary USB-MIDI: interface
2 is subclass `03` (MIDI Streaming) with a standard bulk endpoint pair.

```
INTERFACE 2  class=ff sub=03 proto=00
    EP 0x03  OUT  BULK  512     MIDI to the synth
    EP 0x82  IN   BULK  512     MIDI from the synth
```

Verify with `python usb_probe.py`.

**3. Get the PTSL SDK**

Not included — Avid licenses it separately and forbids redistribution.

1. Register at [developer.avid.com/scripting](https://developer.avid.com/scripting/)
2. Download the Pro Tools Scripting SDK
3. Extract `PTSL.proto`
4. Point the tool at it:

```
setx PTSL_PROTO_PATH "C:\path\to\PTSL.proto"
```

**4. Install the Node client dependencies**

```
npm install @grpc/grpc-js @grpc/proto-loader
```

Pro Tools runs the PTSL server automatically on `localhost:31416` whenever it's
open — no configuration needed.

---

## Use

Export your song from the Fantom as a **Format 1** Standard MIDI File. Format 0
merges every track into one stream and destroys the per-track separation this
depends on.

```
python fantom_stem.py inspect  song.mid          what's on each part
python fantom_stem.py plan     song.mid          preview, sends nothing
python fantom_stem.py run      song.mid --usb --per-track --protools
```

Or use the console:

```
.\Fantom-Capture.ps1 -Song song.mid
```

`.\Install-Shortcut.ps1` puts it on the desktop.

### Options that matter

| | |
|---|---|
| `--per-track` | One Pro Tools track per part, each from timeline zero |
| `--loops N` | Iterations per part. Keep the last — by then reverb tails have reached steady state |
| `--tail N` | Seconds to keep recording after the last note, for tails |
| `--region 9-16` | Capture a bar range. Controllers set earlier are chased in |
| `--clock` | Send MIDI clock so arpeggiator / RPS parts stay in tempo |

### A warning about `--clock`

Clock alone is safe. **Do not enable `--clock-start`** unless you mean it: with
the synth in slave sync, MIDI Start launches *its* sequencer, playing the whole
song underneath the part you're isolating. Arpeggiators need clock, not Start.

---

## The console

Three themes over the same state, cycled with **F8** at any time, including
mid-capture.

- **TURBO** — Borland Turbo Vision. Every control one keystroke away.
- **PHOSPHOR** — amber CRT. Big numbers, readable across a room mid-pass.
- **ANSI** — 16-colour BBS art. Colour-coded columns for the results table.

| Key | |
|---|---|
| `F2` `F3` | Load song / set bar region |
| `F5` `F6` | Test USB link / arm Pro Tools track |
| `F8` | Cycle theme |
| `F9` | Run capture |
| `Esc` | Abort |

Needs Windows Terminal — PHOSPHOR uses 24-bit colour.

---

## Checking the results

You can't hear a capture that ran while you were out of the room, so `tools/`
reports what listening would have told you:

```
python tools/check_audio.py   "C:\path\to\session"     peak / RMS per file
python tools/diagnose_wav.py  take.wav                 clipping, DC, energy map
python tools/ac_content.py    take.wav                 real audio, or DC and hum?
python tools/spectrum.py      take.wav                 hum vs drone vs music
python tools/audio_path_test.py                        is audio reaching the DAW at all?
```

`audio_path_test.py` is the one to run first if something seems wrong. It
records silence, then a chord on each of the 16 channels, and tells you whether
the analogue path exists — a question worth answering before debugging anything
subtler.

---

## Known limitations

**Arpeggiator and RPS parts.** An SMF may hold only the trigger chord rather
than the resulting pattern. `inspect` flags parts with suspiciously few notes.
`--clock` usually recovers them.

**Capture runs in real time.** Twenty parts of eight bars at 122 BPM is about
twelve minutes. The win is that it's unattended, not that it's fast.

**Levels are yours to set.** The tool measures and reports clipping but cannot
fix it — gain staging happens in the analogue domain.

---

## Files

| | |
|---|---|
| `fantom_stem.py` | SMF parsing, scheduling, capture |
| `usb_midi.py` | USB-MIDI transport over libusb |
| `ptools.js` | PTSL client — tracks, transport, markers |
| `Fantom-Capture.ps1` | Themed console |
| `usb_probe.py` `usb_diag.py` `panic.py` | Connection tools |
| `tools/` | Audio verification |

`panic.py` sends MIDI Stop and all-notes-off on every channel. Keep it handy.

---

## Licence

MIT. Not affiliated with Avid or Roland. The PTSL SDK is Avid's and is not
redistributed here.
