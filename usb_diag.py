#!/usr/bin/env python3
"""
usb_diag.py - One-shot diagnostic for the Fantom over raw USB.

Answers three questions in a single run:

  1. Can we open the device and claim the MIDI interface?
  2. Does the Fantom SEND us standard USB-MIDI packets when you play keys?
     This is the decisive one -- it proves the packet format independently
     of any patch, volume or part assignment on the synth.
  3. Does the Fantom RESPOND to notes we send, on any of the 16 channels?

Raw bytes are printed for anything received, so if Roland's framing turns
out to be non-standard the actual wire format is visible rather than guessed.
"""

import sys
import time

import mido

from usb_midi import RolandUsbMidiOut, UsbMidiError, decode

LISTEN_SECONDS = 20
NOTE = 60
VELOCITY = 100


def describe_midi(data):
    """Human-readable summary of a raw MIDI byte-string."""
    if not data:
        return "(empty)"
    try:
        msg = mido.Message.from_bytes(list(data))
        return str(msg)
    except Exception:
        return "unparsed: " + " ".join("%02X" % b for b in data)


def test_receive(port):
    print("-" * 68)
    print("TEST 1  RECEIVE  --  the decisive test")
    print("-" * 68)
    if port.ep_in is None:
        print("  No IN endpoint available. Skipping.")
        return None
    print("  Listening on endpoint 0x%02X for %d seconds." % (
        port.ep_in.bEndpointAddress, LISTEN_SECONDS))
    print()
    print("  >>> PLAY SOME KEYS ON THE FANTOM NOW <<<")
    print()

    deadline = time.time() + LISTEN_SECONDS
    packets = 0
    parsed = 0
    unparsed = 0

    while time.time() < deadline:
        raw, events = port.read(timeout_ms=200)
        if raw is None:
            continue
        packets += 1
        print("  RAW  " + " ".join("%02X" % b for b in raw))
        for cable, data in events:
            if data is None:
                unparsed += 1
                print("       cable %d  <unrecognised code index>" % cable)
            else:
                parsed += 1
                print("       cable %d  %s" % (cable, describe_midi(data)))

    print()
    if packets == 0:
        print("  RESULT: nothing received.")
        print("  Either the Fantom isn't transmitting over USB, or no keys were")
        print("  pressed. Not conclusive on its own -- check test 2.")
        return False
    if parsed:
        print("  RESULT: received %d packet(s), %d decoded as valid MIDI." % (packets, parsed))
        print("  The Fantom speaks standard USB-MIDI event packets. Protocol CONFIRMED.")
        return True
    print("  RESULT: received %d packet(s) but none decoded as standard USB-MIDI" % packets)
    print("  (%d unrecognised). The raw bytes above show the actual framing." % unparsed)
    return False


def test_send(port):
    print()
    print("-" * 68)
    print("TEST 2  SEND  --  note %d on each channel in turn" % NOTE)
    print("-" * 68)
    print("  Listen for which channel produces sound. Each is held ~0.8s.")
    print()
    for ch in range(16):
        print("    channel %2d ..." % (ch + 1), end="", flush=True)
        try:
            port.send(mido.Message("note_on", channel=ch, note=NOTE, velocity=VELOCITY))
            time.sleep(0.8)
            port.send(mido.Message("note_off", channel=ch, note=NOTE, velocity=0))
            print(" sent")
        except Exception as e:
            print(" FAILED: %s" % e)
            return False
        time.sleep(0.15)

    # leave everything silent
    for ch in range(16):
        for cc in (120, 123):
            port.send(mido.Message("control_change", channel=ch, control=cc, value=0))
    print()
    print("  All 16 channels sent without USB error.")
    return True


def main():
    print()
    print("=" * 68)
    print("FANTOM G  --  RAW USB DIAGNOSTIC")
    print("=" * 68)

    try:
        port = RolandUsbMidiOut()
    except UsbMidiError as e:
        print("  FAILED to open: %s" % e)
        return 1

    with port:
        print("  Opened:      %s" % port.describe())
        print("  OUT endpoint: 0x%02X" % port.ep.bEndpointAddress)
        print("  IN endpoint:  %s" % (
            "0x%02X" % port.ep_in.bEndpointAddress if port.ep_in else "none"))
        print()

        received = test_receive(port)
        sent_ok = test_send(port)

    print()
    print("=" * 68)
    print("SUMMARY")
    print("=" * 68)
    print("  Device opens and interface claims:  YES")
    print("  Fantom transmits decodable USB-MIDI: %s" % (
        "YES" if received else "NO / not observed"))
    print("  All 16 channels sent without error:  %s" % ("YES" if sent_ok else "NO"))
    print()
    if received:
        print("  Protocol is confirmed standard USB-MIDI. If you heard nothing in")
        print("  test 2, the issue is on the Fantom side -- part assignment, local")
        print("  control, or MIDI receive switches -- not the transport.")
    else:
        print("  Report whether you heard anything in test 2. Sound with no receive")
        print("  still means it works. Neither means the framing needs more digging,")
        print("  and the raw bytes above are the place to start.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
