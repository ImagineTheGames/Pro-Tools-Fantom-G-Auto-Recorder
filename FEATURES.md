# What this tool already does

Read this before building anything. Most of what looks missing is here
already, usually behind a subcommand or a single key.

This list is derived from the code, not from memory. To regenerate it:

    python fantom_stem.py --help
    grep -n 'sub.add_parser' fantom_stem.py
    grep -n '^    def ' protools.py audio.py
    grep -n 'case "' ptools.js

---

## Trimming heads to the first transient

**This exists. Do not rebuild it.**

| Where | What |
| --- | --- |
| `fantom_stem.py tab` | Tab to transient, split, delete left, shift left — on every track |
| `fantom_stem.py align` | Trim the capture lead so bar 1 lands on bar 1 |
| Console key `A` | Runs `tab` with `--grid`, shows a dry run, asks before applying |
| `protools.py separate_head()` | Separate at a sample, drop the left side, pack right to zero |
| `protools.py trim_head()` | Trim N samples off the head, verified against the EDL |
| `audio.py Take.transient()` | Attack detection in the recorded file |
| `audio.py Take.tab_to_transient()` | Snap to the attack the way Pro Tools' Tab does |
| `audio.py Take.onset()` | First sample above the noise floor |

Notes that matter:

- The trim runs in **Shuffle mode**, where deleting the head ripples
  everything after it left. That is one operation, not separate → delete →
  drag.
- `--grid <song.mid>` keeps parts that do not start on beat 1 where they
  belong. Without it, a part whose first note is a beat into the loop gets
  pulled to zero and plays early against everything else.
- `align` trims by the **smallest** lead in the cluster, never the mean, so
  it can never cut into the earliest part.
- Detection reads the recorded WAV. It does not depend on driving the
  Pro Tools UI.

## Capture

| Command | What |
| --- | --- |
| `plan song.mid` | Preview the layout and write the cue sheet. Sends no MIDI |
| `run song.mid` | Perform the capture pass |
| `run --per-track` | One Pro Tools track per part, each starting at timeline zero |
| `inspect song.mid` | Per-part channel, note count, bars, programs, CCs; flags arp/RPS parts |
| `sweep` | Play a figure on each channel to find which parts respond |
| `test --channel N` | Short four-note run to prove the cable and mapping |
| `ports` | List MIDI ports |
| `transport start\|stop\|continue` | Drive the Fantom's own sequencer |

`--per-track` forces `--lead 0` and `--pt-preroll 0.3` unless asked
otherwise, because per-track takes all start at zero and a long lead is just
silence at the head of every stem.

## Verifying takes

You cannot listen to an unattended pass, so measure it.

| Where | What |
| --- | --- |
| `fantom_stem.py verify` | Measure the recorded stems |
| `fantom_stem.py session` | Show what is actually on the Pro Tools timeline |
| Console key `F7` | Runs `verify` |
| `audio.py Take.report()` | Peak, RMS, crest, noise floor, clipping, character |
| `audio.py Take.has_audio()` | Did anything land at all |
| `audio.py Take.energy_map()` | Coarse shape of the take |
| `protools.py clips()` / `extents()` | The session EDL — the only honest witness to an edit |

## Stopping and recovery

| Where | What |
| --- | --- |
| `Stop-Capture.ps1` | Stop transport, disarm, kill orphans, silence the synth |
| Console key `S` | Same, from inside the console |
| `fantom_stem.py panic` / `panic.py` | MIDI stop, all-sound-off, 128 note-offs on 16 channels |
| `protools.py disarm_all()` | Disarm every audio track |

Order in `Stop-Capture.ps1` is deliberate: Pro Tools first (it damages
sessions), processes second, MIDI panic last. The running capture owns the
Fantom's USB endpoint, so `panic.py` cannot open it until that process is
gone.

## Session and marker handling

| Where | What |
| --- | --- |
| `fantom_stem.py markers` | List or remove memory locations |
| `ptools.js markers-from-cues` | Markers from a cue CSV |
| `protools.py marker()` / `markers()` / `clear_markers()` | Marker API |
| `protools.py ensure_track()` | Create a track only if absent |
| `protools.py locate()` | Park the playhead at a sample |
| `protools.py edit_mode()` | Set Slip / Shuffle / Spot / Grid |

## PTSL client (`ptools.js`)

Runs two ways. `protools.py` starts it in **serve** mode and speaks JSON over
stdin/stdout, which keeps one registered PTSL session alive.

Serve commands: `arm`, `clear-markers`, `clip-extent`, `disarm-all`,
`edit-mode`, `edl`, `ensure-track`, `locate`, `marker`, `markers`, `quit`,
`record`, `redo`, `select-clips`, `separate-head`, `shift-left`, `stop`,
`tempo`, `tracks-state`, `transport`, `trim-head`, `undo`.

One-shot CLI: `info`, `tracks`, `create-track`, `select`, `record-arm`,
`input-monitor`, `transport-arm`, `armed`, `play`, `record`, `stop`,
`transport`, `locate`, `ensure-track`, `disarm-all`, `marker`, `markers`,
`clear-markers`, `markers-from-cues`.

`record` and `stop` read the transport state before acting. Both are the same
toggle in PTSL, so firing blind starts what you meant to stop.

## Console (`Fantom-Capture.ps1`)

    [F2] song   [F3] region  [F5] device  [F6] arm    [F7] verify
    [A]  trim   [L] loops    [C] clock    [F9] capture
    [S]  stop   [F10] quit   [F] follow   arrows/PgUp/PgDn/Home/End scroll

## Hardware and diagnostics

| File | What |
| --- | --- |
| `usb_midi.py` | Raw USB MIDI out, no vendor driver |
| `usb_probe.py`, `usb_diag.py` | Find and diagnose the interface |
| `tools/audio_path_test.py` | Records silence then a chord per channel — run this first when something seems wrong |
| `tools/check_audio.py`, `diagnose_wav.py`, `spectrum.py`, `ac_content.py` | Take inspection |
| `make_test_smf.py` | Generate a test SMF |

---

## Known gaps

- Nothing here trims **imported** material safely. The trim assumes a
  recorded take whose head is silence.
- `ptools.js` needs `node_modules` beside it (`npm install`, see README).
- Windows only: raw USB MIDI plus PTSL on localhost.
