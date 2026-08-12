#!/usr/bin/env python3
"""
usb_midi.py - Direct USB-MIDI transport for vendor-class Roland devices.

The Fantom-G presents a vendor-specific USB interface (class FF / sub 02 /
proto 02) rather than the standard Audio/MIDI-Streaming class, which is why
Windows needs a vendor driver for it. Roland gear of this generation generally
carries ordinary USB-MIDI 4-byte event packets on those endpoints anyway --
only the descriptor class code is non-standard. The Linux kernel treats the
Fantom-X sibling exactly that way (QUIRK_MIDI_FIXED_ENDPOINT).

This module talks to the bulk OUT endpoint directly via libusb, so no kernel
driver is involved. Requires WinUSB (or libusb-win32) bound to the device.

If the assumption above is wrong for the G, `probe()` will still succeed but
notes won't sound -- that is the signal that the packet format differs.
"""

import time

import usb.core
import usb.util

try:
    import libusb_package
    _BACKEND = libusb_package.get_libusb1_backend()
except ImportError:
    _BACKEND = None

VID, PID = 0x0582, 0x00DE


class UsbMidiError(RuntimeError):
    pass


def _encode(msg, cable=0):
    """
    Convert a mido message into one or more 4-byte USB-MIDI event packets.

    Packet layout:  [cable<<4 | CIN, status, data1, data2]
    For channel voice messages CIN is simply the high nibble of the status
    byte, which is what makes this encoding cheap.
    """
    data = msg.bytes()
    status = data[0]
    head = cable << 4
    packets = []

    if status < 0xF0:
        # channel voice message: CIN == status high nibble
        cin = status >> 4
        packets.append(bytes([
            head | cin,
            status,
            data[1] if len(data) > 1 else 0,
            data[2] if len(data) > 2 else 0,
        ]))

    elif status >= 0xF8:
        # system real-time: single byte, CIN 0xF
        packets.append(bytes([head | 0x0F, status, 0, 0]))

    elif status == 0xF0:
        # sysex: 3 bytes per packet (CIN 4), terminated by CIN 5/6/7
        body = list(data)
        while len(body) > 3:
            packets.append(bytes([head | 0x04] + body[:3]))
            body = body[3:]
        cin = {1: 0x05, 2: 0x06, 3: 0x07}[len(body)]
        body += [0] * (3 - len(body))
        packets.append(bytes([head | cin] + body))

    else:
        # system common (0xF1-0xF7)
        cin = {1: 0x05, 2: 0x02, 3: 0x03}.get(len(data), 0x05)
        padded = list(data) + [0] * (3 - len(data))
        packets.append(bytes([head | cin] + padded[:3]))

    return packets


#: bytes of MIDI data carried by each Code Index Number
_CIN_LENGTH = {
    0x02: 2, 0x03: 3, 0x04: 3, 0x05: 1, 0x06: 2, 0x07: 3,
    0x08: 3, 0x09: 3, 0x0A: 3, 0x0B: 3, 0x0C: 2, 0x0D: 2,
    0x0E: 3, 0x0F: 1,
}


def decode(data):
    """
    Turn raw bytes from a USB-MIDI bulk IN endpoint into MIDI byte-strings.

    Returns a list of (cable, bytes). Anything that doesn't parse as a
    USB-MIDI event packet is reported as None so callers can tell the
    difference between "no data" and "data in an unexpected format".
    """
    out = []
    for i in range(0, len(data) - 3, 4):
        packet = bytes(data[i:i + 4])
        if packet == b"\x00\x00\x00\x00":
            continue
        cable = packet[0] >> 4
        cin = packet[0] & 0x0F
        length = _CIN_LENGTH.get(cin)
        if length is None:
            out.append((cable, None))
        else:
            out.append((cable, packet[1:1 + length]))
    return out


class RolandUsbMidiOut:
    """MIDI output over a raw USB bulk endpoint. Mirrors mido's port API."""

    def __init__(self, vid=VID, pid=PID, interface=None, endpoint=None, cable=0):
        self.cable = cable
        self.dev = usb.core.find(idVendor=vid, idProduct=pid, backend=_BACKEND)
        if self.dev is None:
            raise UsbMidiError(
                "Device %04x:%04x not found by libusb. Is WinUSB bound to it?" % (vid, pid))

        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass  # often already configured

        cfg = self.dev.get_active_configuration()
        self.intf, self.ep = self._pick_endpoint(cfg, interface, endpoint)
        if self.ep is None:
            raise UsbMidiError("No bulk OUT endpoint found on this device.")

        try:
            usb.util.claim_interface(self.dev, self.intf.bInterfaceNumber)
        except usb.core.USBError as e:
            raise UsbMidiError("Could not claim interface %d: %s"
                               % (self.intf.bInterfaceNumber, e))

        # An IN endpoint on the same interface lets us hear the Fantom talk
        # back, which is the cleanest proof that the packet format is right.
        self.ep_in = None
        for ep in self.intf:
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                if usb.util.endpoint_type(ep.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK:
                    self.ep_in = ep
                    break

    @staticmethod
    def _pick_endpoint(cfg, want_intf, want_ep):
        """Prefer a bulk OUT on a vendor-specific or MIDI-streaming interface."""
        best = (None, None)
        for intf in cfg:
            if want_intf is not None and intf.bInterfaceNumber != want_intf:
                continue
            interesting = (intf.bInterfaceClass == 0xFF or
                           (intf.bInterfaceClass == 0x01 and
                            intf.bInterfaceSubClass == 0x03))
            for ep in intf:
                if usb.util.endpoint_direction(ep.bEndpointAddress) != usb.util.ENDPOINT_OUT:
                    continue
                if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                    continue
                if want_ep is not None and ep.bEndpointAddress != want_ep:
                    continue
                if interesting:
                    return intf, ep
                if best[1] is None:
                    best = (intf, ep)
        return best

    def send(self, msg):
        """
        Write one message, recovering from a stalled endpoint.

        A bulk endpoint can halt (libusb errno 32, "Pipe error") -- typically
        after an idle gap, which a per-track capture has plenty of while Pro
        Tools is being driven between parts. The stall is clearable; the fix is
        to clear it and retry rather than let a whole pass die on one packet.
        """
        for packet in _encode(msg, self.cable):
            for attempt in (0, 1, 2):
                try:
                    self.ep.write(packet, timeout=1000)
                    break
                except usb.core.USBError as e:
                    stalled = (getattr(e, "errno", None) == 32) or ("pipe" in str(e).lower())
                    if not stalled or attempt == 2:
                        raise
                    try:
                        self.dev.clear_halt(self.ep.bEndpointAddress)
                    except Exception:
                        pass
                    time.sleep(0.002)

    def read(self, timeout_ms=200):
        """
        Poll the IN endpoint. Returns (raw_bytes, decoded) or (None, []) on
        timeout. A timeout is normal and just means the Fantom sent nothing.
        """
        if self.ep_in is None:
            raise UsbMidiError("No bulk IN endpoint on this interface.")
        try:
            data = self.ep_in.read(self.ep_in.wMaxPacketSize, timeout=timeout_ms)
        except usb.core.USBTimeoutError:
            return None, []
        except usb.core.USBError as e:
            if "timeout" in str(e).lower():
                return None, []
            raise
        return bytes(data), decode(data)

    def close(self):
        try:
            usb.util.release_interface(self.dev, self.intf.bInterfaceNumber)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def describe(self):
        return "USB %04x:%04x interface %d endpoint 0x%02x" % (
            self.dev.idVendor, self.dev.idProduct,
            self.intf.bInterfaceNumber, self.ep.bEndpointAddress)


if __name__ == "__main__":
    import time
    import mido

    print("Opening Fantom over raw USB ...")
    with RolandUsbMidiOut() as port:
        print("  ", port.describe())
        print("Playing four notes on channel 1.")
        for note in (60, 64, 67, 72):
            port.send(mido.Message("note_on", channel=0, note=note, velocity=100))
            time.sleep(0.3)
            port.send(mido.Message("note_off", channel=0, note=note, velocity=0))
        for cc in (120, 123):
            port.send(mido.Message("control_change", channel=0, control=cc, value=0))
    print("Done.")
