"""Command-line interface for UT8804E USB measurement and control."""

from __future__ import annotations

import argparse
import json
import signal
from dataclasses import asdict
from pathlib import Path

from .meter import Meter, MeterRejected, MeterTimeout
from .protocol import SUBMODES, Function
from .transport import Cp2110Transport, discover_devices, validate_timeout


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device", help="Opaque HID path from list; otherwise discover one CP2110"
    )
    parser.add_argument("--serial", help="Select the CP2110 USB serial number")
    parser.add_argument("--timeout", type=validate_timeout, default=3.0)
    parser.add_argument(
        "--trace", type=Path, help="Save raw HID reports and decoded events as JSONL"
    )
    parser.add_argument(
        "--allow-storage-writes",
        action="store_true",
        help="Permit explicit save/delete/record-start/stop operations",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="List USB bridges without opening or changing them")
    sub.add_parser("functions", help="List function selectors and submodes offline")
    read = sub.add_parser(
        "read", help="Stream all decoded primary/auxiliary/status information"
    )
    read.add_argument("--count", type=int)
    read.add_argument("--seconds", type=validate_timeout)
    sub.add_parser(
        "status", help="Read one complete display state and bridge FIFO status"
    )
    fun = sub.add_parser(
        "function",
        help="Request a function/submode; ER means rejected, physical knob may gate it",
    )
    fun.add_argument("name", choices=[f.name.lower() for f in Function])
    fun.add_argument("--submode", type=int, default=1)
    rang = sub.add_parser(
        "range", help="Select auto or a function-specific manual range index"
    )
    rang.add_argument("index", help="auto or positive integer index")
    hold = sub.add_parser("hold")
    hold.add_argument("state", choices=["on", "off", "toggle"])
    rel = sub.add_parser("relative")
    rel.add_argument("state", choices=["on", "off"])
    rel.add_argument(
        "--reference",
        type=float,
        default=0.0,
        help="Reference in current transmitted measurement units",
    )
    mm = sub.add_parser("minmax")
    mm.add_argument("state", choices=["on", "off", "restart"])
    live = sub.add_parser("live")
    live.add_argument("state", choices=["on", "off"])
    sub.add_parser("save", help="Save the current reading in the meter's storage")
    sub.add_parser("saved-count")
    sub.add_parser("record-count")
    saved = sub.add_parser("saved-read")
    saved.add_argument("index", type=int)
    header = sub.add_parser("record-header")
    header.add_argument("index", type=int)
    download = sub.add_parser("record-download")
    download.add_argument("index", type=int)
    download.add_argument("--max-samples", type=int, default=1000000)
    for name in ("saved-delete", "record-delete"):
        delete = sub.add_parser(name)
        delete.add_argument("index", help="Index or the explicit word all")
    start = sub.add_parser("record-start")
    start.add_argument("name")
    start.add_argument("--interval-seconds", type=int, required=True)
    start.add_argument("--duration-minutes", type=int, required=True)
    sub.add_parser("record-stop")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)

    def output(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)

    if args.action == "list":
        try:
            for device in discover_devices():
                output(asdict(device))
        except OSError as error:
            import sys

            print(f"UT8804E: {error}", file=sys.stderr)
            return 1
        return 0
    if args.action == "functions":
        for function in Function:
            output(
                {
                    "name": function.name.lower(),
                    "base": f"0x{function:04X}",
                    "submodes": {
                        i + 1: name for i, name in enumerate(SUBMODES[function])
                    },
                }
            )
        return 0
    trace_file = None
    if args.trace:
        trace_file = args.trace.open("w")

    def trace(event):
        if trace_file:
            trace_file.write(
                json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
            )
            trace_file.flush()

    def interrupt(*_):
        raise KeyboardInterrupt

    old_sigterm = signal.signal(signal.SIGTERM, interrupt)
    try:
        with Cp2110Transport(args.device, args.serial, trace) as bridge:
            meter = Meter(
                bridge,
                timeout=args.timeout,
                allow_storage_writes=args.allow_storage_writes,
                trace=trace,
            )
            action = args.action
            if action == "read":
                count = (
                    args.count
                    if args.count is not None
                    else (10 if args.seconds is None else None)
                )
                received = 0
                for reading in meter.readings(count, args.seconds):
                    output(reading)
                    received += 1
                if received == 0:
                    raise MeterTimeout("No meter readings received")
            elif action == "status":
                output({"display": meter.read(), "bridge": bridge.status()})
            elif action == "function":
                output(meter.select_function(Function[args.name.upper()], args.submode))
            elif action == "range":
                output(
                    meter.set_range(None if args.index == "auto" else int(args.index))
                )
            elif action == "hold":
                output(
                    meter.toggle_hold()
                    if args.state == "toggle"
                    else meter.set_hold(args.state == "on")
                )
            elif action == "relative":
                output(meter.relative(args.state == "on", args.reference))
            elif action == "minmax":
                output(meter.minmax(args.state != "off"))
            elif action == "live":
                meter.live(args.state == "on")
                output(
                    {
                        "sent": True,
                        "streaming_requested": args.state == "on",
                        "acknowledged": False,
                    }
                )
            elif action == "save":
                output(meter.save_reading())
            elif action == "saved-count":
                output(meter.saved_count())
            elif action == "record-count":
                output(meter.record_count())
            elif action == "saved-read":
                output(meter.read_saved(args.index))
            elif action == "record-header":
                output(meter.record_header(args.index))
            elif action == "record-download":
                for block in meter.download_record(args.index, args.max_samples):
                    output(block)
            elif action == "saved-delete":
                output(
                    meter.delete_saved(None if args.index == "all" else int(args.index))
                )
            elif action == "record-delete":
                output(
                    meter.delete_record(
                        None if args.index == "all" else int(args.index)
                    )
                )
            elif action == "record-start":
                output(
                    meter.start_recording(
                        args.name, args.interval_seconds, args.duration_minutes
                    )
                )
            elif action == "record-stop":
                output(meter.stop_recording())
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, MeterRejected, MeterTimeout, TimeoutError) as error:
        import sys

        print(f"UT8804E: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
        if trace_file:
            trace_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
