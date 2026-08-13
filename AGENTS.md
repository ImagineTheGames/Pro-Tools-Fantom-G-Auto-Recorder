# Working on this project

Unattended per-part stem capture: a Roland Fantom-G is driven over raw USB
MIDI while Pro Tools records each part to its own track, then each stem's
head is trimmed so every take starts on its first note.

**Read `FEATURES.md` before writing code.** Most things that look missing
already exist. That file is a map of the surface; this one is how to work
here without breaking the session.

---

## The code that actually runs

    fantom-stem/
      Fantom-Capture.ps1   console front end (keys listed in FEATURES.md)
      fantom_stem.py       every subcommand: plan, run, tab, align, verify...
      protools.py          persistent PTSL connection, verified edits
      audio.py             attack detection and take measurement
      ptools.js            PTSL client; protools.py runs it in `serve` mode
      usb_midi.py          raw USB MIDI transport
      panic.py             silence the synth
      Stop-Capture.ps1     stop a crashed or runaway capture
      tools/               take inspection utilities

Entry points: `.\Fantom-Capture.ps1 -Song song.mid`, or
`python fantom_stem.py <command>`, or `.\fs <command>`.

### Verify this before trusting it

This directory was unversioned for a long time while a **stale copy** sat on
GitHub. An agent read the GitHub copy, assumed it was current, and spent an
afternoon rebuilding trimming that already existed here — the published
`fantom_stem.py` was 43 KB against 64 KB here, with no `protools.py` and no
`audio.py` at all.

So: **find the code that executes, then read it.** Cheap checks —

    grep -n 'sub.add_parser' fantom_stem.py     # real command list
    grep -n 'PTOOLS_JS' protools.py             # which client is used
    node ptools.js info                         # does it connect
    git log --oneline -5                        # is this current

If a path in a doc points outside this directory, confirm it still exists
before relying on it.

---

## Hard-won facts

Each of these cost real time or real damage. None are hypothetical.

### PTSL reports success for edits that hit the wrong track

Track selection and **edit** selection are different things.
`SelectTracksByName` sets the former. Cut, Clear and Paste follow the latter,
which only follows track selection when *Options → Link Track and Edit
Selection* is on. With it off, edits land on whatever track the cursor was
on, and every API call still returns ok.

An early attempt at trimming separated `20 Track 20` three times while
reporting success on track 6.

`protools.py` handles this: every destructive method verifies its target
before acting and its result after.

### The EDL is the only witness

Trimming a clip never changes the WAV on disk, and return values lie (see
above). Neither the audio files nor the API response can confirm an edit
happened. `Session.clips()` / `extents()` read the session EDL. Use them.

### Do not test destructive operations on a live session

Testing a trim against the open session wiped the clips off all 24 tracks.
It was recoverable only because Pro Tools' undo stack held, and because
nothing had been saved.

- Never save a session you did not open.
- Prefer `--dry-run` (`tab` has one; `verify` and `session` are read-only).
- If something goes wrong, undo, then check `Session.clips()` before
  undoing further. Undoing past your own edits reverts the user's work.

### Tab and transport traps

- Tab stops at **clip boundaries** as well as transients. Tabbing from 0 on a
  clip that starts at sample 1 lands on 1 — the clip start, not the attack.
- `GetSessionLength` returns a **timecode string** (`"24:00:00:00"`), not
  samples. `parseInt` on it gives 24.
- Transport states serialise as `TState_...`, not the `TS_...` spelling used
  for the enum in `PTSL.proto`. Matching the proto spelling matches nothing.
- `stop` and `play` are the same toggle. Read `GetTransportState` first, or
  "stop" starts playback.
- `TState_TransportIsCued` is a staging state; returning while it is set
  means the first notes play before the tape is moving.

### The synth holds the USB port

`panic.py` cannot open the Fantom while a capture process is alive — it
fails with "Access denied", exactly when you need it. Kill the capture
first, then panic. `Stop-Capture.ps1` does this in the right order.

If USB is unavailable there is nothing software can do: press STOP on the
instrument or turn it down.

### Driving the Pro Tools UI directly (last resort)

PTSL has no Tab to Transient and no Edit menu commands. Both are reachable
through Win32, and this tool does **not** depend on that — but if you ever
add it:

- `SendInput` events must populate `wVk`. Scan-code-only events are silently
  ignored by Pro Tools.
- Keystrokes must target the MDI edit window (`DigiMDIWndClass`, titled
  `Edit: <session>`), which is a *child* of the app frame and therefore
  invisible to `EnumWindows`. Use `EnumChildWindows`.
- Menu commands go to the app frame (`DigiAppWndClass`) via `WM_COMMAND`.
- The Edit menu's "Undo <op>" label is the cheapest proof a command landed.

Prefer the audio-analysis path in `audio.py`: it needs no focus, no window,
and works while you are typing elsewhere.

---

## Conventions

- Comments explain **why**, especially where the obvious approach is wrong.
  The files above are written that way; match them.
- Recorded audio never enters the repo (`.gitignore` covers `*.wav`).
- Node dependencies are pinned in `package.json`; `ptools.js` resolves them
  from this directory.
- Windows only. PowerShell for the front end, Python for the work, Node for
  PTSL.

## When you finish something

Say plainly what was verified and what was not. "Tested against a live
session" and "it compiles" are different claims, and the difference here is
someone's song.
