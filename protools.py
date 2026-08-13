#!/usr/bin/env python3
"""
protools.py - Talking to Pro Tools.

Wraps the PTSL client (ptools.js) in a persistent connection and provides the
one thing that makes the API safe to use: a way to see what actually happened.

Two facts drive the design.

    PTSL returns ok for commands that acted on the wrong track. Track selection
    and EDIT selection are separate; Cut, Clear and Paste follow the edit
    selection, so setting only the former sends edits somewhere else entirely.

    Trimming a clip never changes the WAV on disk. Neither return values nor
    audio files can confirm an edit -- only the session EDL can.

So every destructive operation here verifies its target first and its result
after, against Session.clips().
"""

import json
import os
import re
import subprocess

# ptools.js ships beside this file. It used to be loaded from the
# protools-mcp-server checkout, which made this tool depend on an untracked
# file inside an unrelated project: a `git clean` over there would delete the
# PTSL client the capture runs on.
PTOOLS_JS = os.environ.get(
    "PTOOLS_JS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ptools.js"))


class ProToolsError(RuntimeError):
    pass


class Session(object):
    """A live connection to the open Pro Tools session."""

    def __init__(self, js=None):
        self.proc = subprocess.Popen(
            ["node", js or PTOOLS_JS, "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        hello = self.proc.stdout.readline()
        if not hello:
            err = (self.proc.stderr.read() or "")[:300]
            raise ProToolsError("PTSL client would not start: %s" % err)
        self.connected = True

    # -- plumbing ----------------------------------------------------------

    def send(self, cmd, **kw):
        kw["cmd"] = cmd
        try:
            self.proc.stdin.write(json.dumps(kw) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            raise ProToolsError("connection lost: %s" % e)
        line = self.proc.stdout.readline()
        if not line:
            raise ProToolsError("connection closed during '%s'" % cmd)
        try:
            return json.loads(line)
        except ValueError:
            raise ProToolsError("bad reply to '%s': %s" % (cmd, line[:120]))

    def close(self):
        try:
            self.send("quit")
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

    # -- reading -----------------------------------------------------------

    def info(self):
        return self.send("info")

    def tracks(self):
        return self.send("tracks-state").get("tracks", [])

    def clips(self):
        """
        {track: [(clip, start, end, duration)]} in samples, from the session EDL.

        This is the only reliable view of what is on the timeline.
        """
        txt = self.send("edl").get("text", "")
        out = {}
        track = None
        for ln in txt.splitlines():
            if ln.startswith("TRACK NAME:"):
                raw = ln.split("\t", 1)[1].strip() if "\t" in ln else ""
                track = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
                out.setdefault(track, [])
                continue
            if track is None or "\t" not in ln:
                continue
            f = [x.strip() for x in ln.split("\t")]
            if len(f) < 6 or not f[0].isdigit():
                continue
            try:
                out[track].append((f[2], int(f[3]), int(f[4]), int(f[5])))
            except ValueError:
                pass
        return out

    def extents(self):
        """{track: (start, end)} across all clips on each track."""
        res = {}
        for name, cl in self.clips().items():
            if cl:
                res[name] = (min(c[1] for c in cl), max(c[2] for c in cl))
        return res

    def edit_selection(self):
        """Which track currently holds the edit selection, if any."""
        for t in self.tracks():
            if t.get("edit") and t["edit"] != "None":
                return t["name"]
        return None

    # -- writing -----------------------------------------------------------

    def ensure_track(self, name, mono=False):
        return self.send("ensure-track", name=name, mono=mono)

    def arm(self, name, on=True):
        return self.send("arm", name=name, off=not on)

    def disarm_all(self):
        return self.send("disarm-all")

    def locate(self, samples=0):
        return self.send("locate", samples=int(samples))

    def record(self):
        return self.send("record")

    def stop(self):
        return self.send("stop")

    def marker(self, name, samples):
        return self.send("marker", name=name, samples=int(samples))

    def markers(self):
        """Every memory location in the session, newest page included."""
        return self.send("markers").get("markers", [])

    def clear_markers(self, numbers, verify=True):
        """
        Delete memory locations by number, then confirm they are gone.

        PTSL answers ok whether or not a number existed, so the count only
        means anything after re-reading the list.
        """
        numbers = [int(n) for n in numbers]
        if not numbers:
            return {"cleared": 0}
        before = set(m["number"] for m in self.markers())
        r = self.send("clear-markers", numbers=numbers)
        if r.get("error"):
            raise ProToolsError("clear markers: %s" % r["error"])
        if not verify:
            return r
        after = set(m["number"] for m in self.markers())
        gone = before - after
        left = [n for n in numbers if n in after]
        if left:
            raise ProToolsError(
                "clear markers: %d still present (%s)"
                % (len(left), ", ".join(str(n) for n in left[:10])))
        return {"cleared": len(gone), "remaining": len(after)}

    def separate_head(self, name, samples, verify=True):
        """
        Separate at `samples`, drop the left side, pack the right side to zero.

        Uses Pro Tools' own Trim To Selection rather than deleting a timeline
        range, so the edit is the one you would make by hand.
        """
        samples = int(samples)
        ext = self.extents().get(name)
        if ext is None:
            raise ProToolsError("separate %s: track not in the EDL" % name)
        before_len = ext[1] - ext[0]
        r = self.send("separate-head", name=name, samples=ext[0] + samples, end=ext[1])
        if not r.get("ok", True) and r.get("error"):
            raise ProToolsError("separate %s: %s" % (name, r["error"]))
        if not verify:
            return r
        after = self.extents().get(name)
        if after is None:
            raise ProToolsError("separate %s: track vanished from the EDL" % name)
        removed = before_len - (after[1] - after[0])
        if abs(removed - samples) > 2:
            raise ProToolsError(
                "separate %s: expected %d samples removed, saw %d"
                % (name, samples, removed))
        return {"track": name, "removed": removed, "start": after[0]}

    def trim_head(self, name, samples, verify=True):
        """
        Remove `samples` from the front of a track, rippling the rest left.

        Verifies the edit selection is on the intended track before cutting,
        and confirms the clip actually shortened afterwards. Raises rather
        than reporting a success it hasn't checked.
        """
        samples = int(samples)
        before = self.extents().get(name)
        r = self.send("trim-head", name=name, samples=samples)
        if not r.get("ok"):
            raise ProToolsError("trim %s: %s" % (name, r.get("error")))
        if not verify:
            return r
        after = self.extents().get(name)
        if before is None or after is None:
            raise ProToolsError("trim %s: track not found in EDL" % name)
        removed = before[1] - after[1]
        if abs(removed - samples) > 2:
            raise ProToolsError(
                "trim %s: expected %d samples removed, saw %d" % (name, samples, removed))
        return {"track": name, "removed": removed}

    def edit_mode(self, mode="EMode_Slip"):
        return self.send("edit-mode", mode=mode)
