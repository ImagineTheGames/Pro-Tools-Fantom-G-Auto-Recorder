#!/usr/bin/env python3
"""
panic.py - Stop everything the synth is doing, immediately.

Sends MIDI Stop, then All Sound Off / All Notes Off / Reset Controllers /
sustain-off / pitch-centre on all 16 channels. Safe to run any time.
"""

import sys
import time

try:
    import mido
    from usb_midi import RolandUsbMidiOut, UsbMidiError
except Exception as e:
    sys.exit("import failed: %s" % e)

try:
    port = RolandUsbMidiOut()
except Exception as e:
    print("Could not open the Fantom over USB: %s" % e)
    print()
    print("Nothing here can silence it. Do one of these on the instrument:")
    print("  - press STOP on the Fantom")
    print("  - or turn its volume down")
    print("  - or power-cycle it")
    sys.exit(1)

with port:
    port.send(mido.Message("stop"))
    for ch in range(16):
        port.send(mido.Message("control_change", channel=ch, control=120, value=0))
        port.send(mido.Message("control_change", channel=ch, control=123, value=0))
        port.send(mido.Message("control_change", channel=ch, control=64, value=0))
        port.send(mido.Message("control_change", channel=ch, control=121, value=0))
        port.send(mido.Message("pitchwheel", channel=ch, pitch=0))
    time.sleep(0.05)
    # belt and braces: explicit note-off across the whole range
    for ch in range(16):
        for note in range(128):
            port.send(mido.Message("note_off", channel=ch, note=note, velocity=0))

print("MIDI Stop + all-sound-off + 128 note-offs sent on all 16 channels.")
