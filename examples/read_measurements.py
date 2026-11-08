#!/usr/bin/env python3
"""Print ten valid readings, optionally selecting a USB bridge serial number."""

import argparse

from ut8804e import Cp2110Transport, Meter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", help="USB serial reported by ut8804e list")
    args = parser.parse_args()
    with Cp2110Transport(serial=args.serial) as transport:
        meter = Meter(transport, timeout=3.0)
        for packet in meter.readings(count=10):
            reading = packet["primary"]
            if reading is not None and reading["valid"]:
                print(reading["value"], reading["unit"])
            else:
                print("Invalid reading:", reading)


if __name__ == "__main__":
    main()
