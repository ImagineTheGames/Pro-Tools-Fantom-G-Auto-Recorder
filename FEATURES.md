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

**This exists. Do not rebuild it, and do not measure the audio to do it.**

| Where | What |
| --- | --- |
| **automatic** | A finished `run --per-track` pass trims every stem it recorded. No dry run, no prompt. `--no-trim` opts out |
| `fantom_stem.py tab` | Tab to transient, separate, delete the head, pull to zero. Takes no session path -- it asks Pro Tools |
| Console key `A` | Runs `tab` immediately |
| `ptmcp.py trim_heads()` | The MCP call that does it |
| `ptmcp.py preflight()` | Whether the Windows-level automation can work at all right now |
| `protools.py separate_head()` | Separate at a sample, drop the left side, pack right to zero |
| `protools.py trim_head()` | Trim N samples off the head, verified against the EDL |
| `fantom_stem.py align` | Trim a shared capture lead, using the smallest in the group |

Notes that matter:

- **Pro Tools finds the transient, this tool does not.** PTSL has no Tab to
  Transient, so an earlier version measured the WAV and predicted where
  Pro Tools would land. Tuned to match one hand edit, it cut into the attack
  of the other nineteen. The MCP server drives the real menu command.
- **A focused floating Pro Tools window swallows the Tab keystroke.** Tab
  then does nothing and the transient comes back as wherever the insertion
  already was -- cuts from 21 ms to 0.99 s, every one looking clean.
  `pt_preflight` warns; it no longer refuses, because refusing left a whole
  capture untrimmed while the pass carried on regardless.
- **No head ceiling by default** (`--max-head 0`). Whether a late transient is
  a soft attack or dead air is a judgement about the music.
- Tracks are trimmed **one at a time** and each is checked against the session
  before the next, so a bad run damages one track rather than thirty. A
  server *skip* is a deliberate refusal, not a failure, and does not abort the
  run -- treating it as one once left sixteen tracks untouched.
- `align` trims by the **smallest** lead in the cluster, never the mean, so
  it can never cut into the earliest part.

## Extending to a usable length

| Where | What |
| --- | --- |
| **automatic** | A finished pass repeats the settled loop up to `--minutes` (default 3.5). `--no-extend` opts out |
| `fantom_stem.py extend song.mid` | The same, on demand |
| Console key `B` | Override the loop length for the whole song |
| `fantom_stem.py part_loop_bars()` | How long one part loops |
| `fantom_stem.py song_loop_bars()` | The base length, being the one most parts share |

- Loop length is measured from the last note **START**. A held release
  crossing the final bar line is not another bar of music -- reading the last
  *event* gave a 9 bar loop for 8 bars of music and put a bar of tail inside
  every iteration.
- Parts may differ. Longer parts round up to a whole multiple, shorter parts
  keep their length if it divides the base evenly and is at least half of it.
  Anything else rounds up.
- Groups are measured in units of the **shortest** loop. Rounding each group
  from the tempo separately cannot line up -- an odd sample count halves to
  a fraction and the groups drift apart.
- Copy, paste and clear force **both** edit-selection links on. Without
  `link_timeline` a timeline range is not an edit selection at all; without
  `link_track` it covers one track. A paste once reached 1 of 26 tracks and
  reported success.

## Naming tracks from the Fantom song file

| Where | What |
| --- | --- |
| **automatic** | A finished pass names every track it recorded. `--no-name` opts out |
| `fantom_stem.py name song.mid` | The same, on demand |
| Console key `N` | Runs it |
| `svq.py part_names()` | Patch name per part from a `.SVQ` |
| `svq.py find_svq()` | Locate the song file, matching on alphanumerics only |
| `svq.py track_names()` | Dedup and number repeated patches |
| `svq.py load_overrides()` | `SONG.names` corrections beside the MIDI |

The exported SMF carries no track names, no programs and no banks. The synth
answers an Identity Request but ignores Data Requests, so the file is the
only source.

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

Serve commands: `arm`, `clear-markers`, `clear-range`, `clip-extent`,
`copy-range`, `disarm-all`, `edit-mode`, `edl`, `ensure-track`, `locate`,
`marker`, `markers`, `new-session`, `open-session`, `paste-at`, `quit`,
`record`, `redo`, `rename-track`, `save`, `select-clips`, `separate-head`,
`session-path`, `shift-left`, `spot`, `stop`, `tempo`, `tracks-state`,
`transport`, `trim-head`, `undo`.

`new-session` must pass `input_output_settings` or `CreateSession` reports
success and creates nothing. `spot` accepts invalid clip IDs and silently
does nothing, so it is not a way to restore clips.

One-shot CLI: `info`, `tracks`, `create-track`, `select`, `record-arm`,
`input-monitor`, `transport-arm`, `armed`, `play`, `record`, `stop`,
`transport`, `locate`, `ensure-track`, `disarm-all`, `marker`, `markers`,
`clear-markers`, `markers-from-cues`.

`record` and `stop` read the transport state before acting. Both are the same
toggle in PTSL, so firing blind starts what you meant to stop.

## Console (`Fantom-Capture.ps1`)

    [F2] song   [F3] region  [F5] device  [F6] arm    [F7] verify
    [A]  trim   [N] name     [P] parts    [B] bars    [L] loops
    [C]  clock  [R] record   [S] stop     [F10] quit
    [F]  follow  arrows/PgUp/PgDn/Home/End scroll

`R` records and `S` stops. During a capture, `Esc`, `Q`, `F10`, `Space` and
`Ctrl+C` all stop as well -- mid-pass there was once no way out at all.
`A_RunFantomCapture.bat` launches the console without right-clicking.

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
- Songs cannot be switched on the Fantom remotely: Song Select is ignored.
- Nothing arrives back over the Fantom's own USB port, in either direction
  tested. Use a 5-pin interface if you need MIDI in.
- Empty Fantom tracks are not exported, so part numbers shift against the
  instrument after any gap.
- Session tempo has no PTSL command and must be set by hand.
- `ptools.js` needs `node_modules` beside it (`npm install`, see README).
- Windows only: raw USB MIDI plus PTSL on localhost.
