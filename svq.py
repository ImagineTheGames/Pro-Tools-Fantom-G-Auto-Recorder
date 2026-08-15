#!/usr/bin/env python3
"""
svq.py - Reading part names out of a Fantom-G .SVQ song file.

The SMF the Fantom exports carries note data and nothing else: no program
changes, no bank selects, and no track names at all -- the "Track 1".."Track 20"
you see are invented by this tool as a fallback. The .SVQ, the Fantom's own song
format, does keep the patch name for every part.

Asking the synth directly was tried first and does not work. The Fantom answers
a universal Identity Request over MIDI (F0 7E 7F 06 01 F7 -> Roland, family
27 02, device 0x10) but ignores Roland Data Requests on every model ID and
address tried, which fits this generation moving bulk data to USB file transfer.
So the file is the source.

Layout, worked out by inspection:

    'STrk'  4 bytes
    length  4 bytes, little endian
    +8      6 bytes, flags
    +16     2 bytes, little endian track number, 1-based
    +18    16 bytes, patch name, space padded

Names of tracks recovered from a damaged card sometimes have a NUL where a
character should be -- 'G Standard\\x00Kit'. Byte 18+10 is normally a space or a
letter, so those are corrupted bytes rather than a field boundary. They are
turned back into spaces here, and anything still wrong is what the overrides
file is for.
"""

import collections
import glob
import os
import re

NAME_OFF = 18
NAME_LEN = 16
SKIP = ("Beat Track", "Tempo Track")


def part_names(path):
    """{part number: patch name} for one .SVQ file."""
    with open(path, "rb") as fh:
        data = fh.read()

    found = {}
    for m in re.finditer(b"STrk", data):
        o = m.start() + 8
        if o + NAME_OFF + NAME_LEN > len(data):
            continue
        num = int.from_bytes(data[o + 8:o + 10], "little")
        raw = data[o + NAME_OFF - 8:o + NAME_OFF - 8 + NAME_LEN]
        text = "".join(chr(b) if 32 <= b < 127 else " " for b in raw)
        name = " ".join(text.split())
        if name and name not in SKIP and 1 <= num <= 128:
            found[num] = name
    return found


def find_svq(song_title, folders):
    """
    The .SVQ files for a song title, newest revision first.

    The Fantom writes _v2 / _v3 suffixes for later saves of the same song, and
    an earlier revision can have fewer parts than the arrangement that was
    actually captured, so the one with the most parts wins.
    """
    # Match on letters and digits only. The Fantom decorates song names on the
    # card -- '%tOGEE WIZARDt%.SVQ' for what the exported MIDI calls
    # 'TOGEEWIZARD.mid' -- so a literal comparison never matches.
    def key(s):
        return "".join(c for c in s if c.isalnum()).upper()

    want = key(song_title)
    hits = []
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for path in glob.glob(os.path.join(folder, "*.SVQ")):
            if want and want in key(os.path.basename(path)):
                hits.append(path)
    return sorted(hits, key=lambda p: (len(part_names(p)), p), reverse=True)


def load_overrides(path):
    """
    Corrections, one per line: `3 = Jazz Clean Gtr`.

    Carved files have the odd damaged byte and the Fantom truncates long patch
    names, so there has to be somewhere to fix a name once and keep it.
    """
    fixes = {}
    if not path or not os.path.isfile(path):
        return fixes
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            num, name = line.split("=", 1)
            try:
                fixes[int(num.strip())] = name.strip()
            except ValueError:
                pass
    return fixes


def track_names(names, prefix=True):
    """
    Turn patch names into Pro Tools track names.

    A name that appears once stays as it is; one that repeats gets numbered, so
    a lone bass is 'Bass' and two guitars are 'ElectricGuitar1' and
    'ElectricGuitar2'.
    """
    counts = collections.Counter(names.values())
    seen = collections.Counter()
    out = {}
    for num in sorted(names):
        patch = names[num]
        clean = "".join(c for c in patch.title() if c.isalnum())
        if not clean:
            continue
        if counts[patch] > 1:
            seen[patch] += 1
            clean += str(seen[patch])
        out[num] = ("%02d %s" % (num, clean)) if prefix else clean
    return out
