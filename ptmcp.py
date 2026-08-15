#!/usr/bin/env python3
"""
ptmcp.py - Talking to the Pro Tools MCP server.

PTSL alone cannot do this job. It has no Tab to Transient, no Separate Clip and
no nudge, so the head trim used to rely on a transient detector written here,
against the recorded audio. That was wrong in a way that took several passes to
see: tuned until it matched one track's manual edit, it then cut into the attack
of the other nineteen. Pro Tools knows where the transient is. Ask it.

The MCP server reaches the parts of Pro Tools PTSL does not expose, by driving
the application's own menu commands. This is the bridge to it: MCP over stdio,
which is the same channel an MCP client uses.
"""

import json
import os
import subprocess

SERVER = os.environ.get(
    "PT_MCP_SERVER", r"C:\Users\Rei\protools-mcp-server\dist\index.js")
PROTO = os.environ.get(
    "PTSL_PROTO_PATH",
    r"C:\ProTools\PTSLSDK\PTSL_SDK_CPP.2026.04.0.1301892\Source\PTSL.proto")


class McpError(RuntimeError):
    pass


class ProToolsMcp(object):
    """One MCP session against the Pro Tools server."""

    def __init__(self, server=None):
        env = dict(os.environ)
        env["PTSL_PROTO_PATH"] = PROTO
        env["ALLOW_WRITES"] = "all"          # read-only by default, and useless so
        self.proc = subprocess.Popen(
            ["node", server or SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
            # The server writes UTF-8; Python would otherwise decode it as the
            # console codepage and die on the first bullet character it prints.
            encoding="utf-8", errors="replace")
        self._id = 0
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "fantom-stem", "version": "1"}})
        self._notify("notifications/initialized", None)

    # -- plumbing ----------------------------------------------------------

    def _send(self, obj):
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            raise McpError("MCP server not accepting input: %s" % e)

    def _notify(self, method, params):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _rpc(self, method, params):
        self._id += 1
        want = self._id
        self._send({"jsonrpc": "2.0", "id": want, "method": method,
                    "params": params})
        # Skip anything that is not the reply we are waiting for: the server
        # interleaves notifications with responses.
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise McpError("MCP server closed during '%s'" % method)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == want:
                if "error" in msg:
                    raise McpError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})

    def call(self, tool, **args):
        """Call a tool and return its text output."""
        r = self._rpc("tools/call", {"name": tool, "arguments": args})
        text = "\n".join(c.get("text", "") for c in r.get("content", []))
        # The server writes bullets and dashes the Windows console cannot
        # render, and they arrive as mojibake in the middle of numbers you are
        # trying to read. Trade them for ASCII.
        for uni, ascii_ in (("•", "*"), ("—", "-"), ("–", "-"),
                            ("‘", "'"), ("’", "'"),
                            ("“", '"'), ("”", '"'), ("→", "->")):
            text = text.replace(uni, ascii_)
        return text

    def close(self):
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

    # -- the parts of Pro Tools PTSL will not reach ------------------------

    def preflight(self):
        """
        Whether the Windows-level automation can work at all right now.

        Every condition it checks -- edit window present, no modal dialog,
        interactive desktop, matching process integrity -- fails SILENTLY at
        runtime. Worth asking before an unattended pass rather than after.
        """
        return self.call("pt_preflight")

    def tab_to_transient(self, track, from_sample=0):
        """Pro Tools' own answer for where the first transient is, in samples."""
        out = self.call("tab_to_transient", track_name=track,
                        from_sample=str(int(from_sample)))
        for word in out.replace(",", " ").split():
            if word.isdigit():
                return int(word)
        return None

    # Bigger than any take: the server insists on a ceiling, this is how you
    # say "there isn't one".
    NO_CEILING = 2 ** 31 - 1

    def trim_heads(self, tracks, max_head=0, dry_run=False):
        """
        Tab to the transient, separate there, clear the head, pull to zero.

        max_head=0 means no ceiling, and that is the default: every track is
        trimmed to its transient wherever it falls. The server's own guard
        skips a late transient on the theory that it is a soft attack rather
        than dead air, which is a judgement about the music -- not one this
        tool gets to make on the user's behalf.
        """
        return self.call("trim_head_to_transient", track_names=list(tracks),
                         max_head_samples=int(max_head) or self.NO_CEILING,
                         dry_run=bool(dry_run), move_to_zero=True)
