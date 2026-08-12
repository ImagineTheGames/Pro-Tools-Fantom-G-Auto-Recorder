#!/usr/bin/env python3
"""
usb_probe.py - Inspect the Fantom's USB configuration.

Run this after binding WinUSB to the device (or before, to see how far we get
with only the hub driver). Dumps interfaces and endpoints so we can tell
whether the vendor-specific interface carries standard USB-MIDI event packets.
"""

import sys

import usb.core
import usb.util

try:
    import libusb_package
    BACKEND = libusb_package.get_libusb1_backend()
except ImportError:
    BACKEND = None

VID, PID = 0x0582, 0x00DE

EP_TYPES = {0: "CONTROL", 1: "ISOCHRONOUS", 2: "BULK", 3: "INTERRUPT"}


def safe(fn, default="<unreadable>"):
    try:
        v = fn()
        return v if v is not None else default
    except Exception as e:
        return "%s (%s)" % (default, type(e).__name__)


def main():
    devices = list(usb.core.find(find_all=True, backend=BACKEND))
    print("Visible USB devices: %d\n" % len(devices))
    print("  VID:PID    Class Sub Prot  Description")
    print("  ---------- ----- --- ----  -----------")
    target = None
    for d in devices:
        mark = ""
        if d.idVendor == VID and d.idProduct == PID:
            target = d
            mark = "   <-- FANTOM G"
        print("  %04x:%04x   %-5s %-3s %-4s  %s%s" % (
            d.idVendor, d.idProduct,
            "%02x" % d.bDeviceClass, "%02x" % d.bDeviceSubClass,
            "%02x" % d.bDeviceProtocol,
            safe(lambda: d.product, "?"), mark))

    if target is None:
        print("\nFantom (%04x:%04x) NOT visible to libusb." % (VID, PID))
        print("It needs a WinUSB/libusb-compatible driver bound before libusb can see it.")
        return 1

    print("\n" + "=" * 70)
    print("FANTOM G  %04x:%04x" % (VID, PID))
    print("=" * 70)
    print("  Manufacturer: %s" % safe(lambda: target.manufacturer))
    print("  Product:      %s" % safe(lambda: target.product))
    print("  Serial:       %s" % safe(lambda: target.serial_number))
    print("  USB version:  %s" % safe(lambda: hex(target.bcdUSB)))
    print("  Configs:      %d" % target.bNumConfigurations)

    for cfg in target:
        print("\n  CONFIGURATION %d  (%d interface(s), %d mA)" % (
            cfg.bConfigurationValue, cfg.bNumInterfaces, cfg.bMaxPower * 2))
        for intf in cfg:
            print("    INTERFACE %d alt %d  class=%02x sub=%02x proto=%02x  (%d endpoints)" % (
                intf.bInterfaceNumber, intf.bAlternateSetting,
                intf.bInterfaceClass, intf.bInterfaceSubClass,
                intf.bInterfaceProtocol, intf.bNumEndpoints))
            if intf.bInterfaceClass == 0x01 and intf.bInterfaceSubClass == 0x03:
                print("        ^ USB AUDIO / MIDI STREAMING class")
            elif intf.bInterfaceClass == 0xFF:
                print("        ^ vendor-specific")
            for ep in intf:
                direction = "IN " if usb.util.endpoint_direction(ep.bEndpointAddress) else "OUT"
                ep_type = EP_TYPES.get(usb.util.endpoint_type(ep.bmAttributes), "?")
                print("        EP 0x%02x  %s  %-10s  maxpacket=%d  interval=%d" % (
                    ep.bEndpointAddress, direction, ep_type,
                    ep.wMaxPacketSize, ep.bInterval))

    print("\n" + "=" * 70)
    print("READ: a BULK OUT endpoint on a vendor-specific interface is what we")
    print("want. Roland gear of this era typically carries standard USB-MIDI")
    print("4-byte event packets on it despite the non-standard class code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
