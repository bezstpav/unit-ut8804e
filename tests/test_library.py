"""Native protocol, simulated-session and transport-restoration tests."""

from __future__ import annotations

import io
import json
import random
import struct
import subprocess
import sys
import unittest
from collections import deque
from contextlib import redirect_stderr
from unittest.mock import patch

from ut8804e import Meter, MeterRejected, MeterTimeout, StorageWriteNotEnabled
from ut8804e.cli import main
from ut8804e.protocol import (
    Command,
    DisplayFlag,
    FrameParser,
    Function,
    ProtocolError,
    Response,
    decode_display,
    decode_packet,
    decode_timestamp,
    decode_value,
    function_payload,
    index_payload,
    pack_command,
    range_payload,
    record_data_payload,
    relative_payload,
    start_recording_payload,
    unpack_frame,
)
from ut8804e.transport import (
    Cp2110Transport,
    Feature,
    TransportError,
    TransportTimeout,
    discover_devices,
)


def unit(text="VDC"):
    return text.encode("ascii").ljust(8, b"\0")


def value(number=1.25, status=0, decimals=4, with_unit=True, text="VDC"):
    return struct.pack("<fB", number, decimals << 4 | status) + (
        unit(text) if with_unit else b""
    )


def timestamp(year=2026, month=11, day=7, hour=21, minute=37, second=55):
    return (
        year - 2000 | month << 6 | day << 10 | hour << 15 | minute << 20 | second << 26
    )


def normal_body(
    flags=DisplayFlag.BARGRAPH | DisplayFlag.AUTO_RANGE, selector=0x3110, range_index=1
):
    body = (
        struct.pack("<BHHB", Response.LIVE_READING, int(flags), selector, range_index)
        + value()
    )
    for flag in (DisplayFlag.AUX1, DisplayFlag.AUX2, DisplayFlag.AUX3):
        if flags & flag:
            body += value(12.5, text="Hz")
    if flags & DisplayFlag.BARGRAPH:
        body += struct.pack("<f", 1.2) + unit()
    return body


def frame_body(body):
    return pack_command(body[0], body[1:])


class ProtocolTests(unittest.TestCase):
    def test_synthetic_primary_voltage(self):
        result = decode_packet(unpack_frame(frame_body(normal_body())))
        self.assertEqual(result["kind"], "live")
        self.assertEqual(result["primary"]["unit"], "VDC")
        self.assertEqual(result["primary"]["value"], 1.25)

    def test_synthetic_frequency(self):
        body = struct.pack("<BHHB", Response.LIVE_READING, 0, 0x7110, 3)
        body += value(1.2345, text="kHz")
        result = decode_packet(unpack_frame(frame_body(body)))
        self.assertEqual(result["function"], "FREQUENCY")
        self.assertEqual(result["primary"]["unit"], "kHz")
        self.assertAlmostEqual(result["primary"]["value"], 1.2345, places=5)

    def test_hidden_or_lead_error_is_not_a_valid_value(self):
        for flag in (DisplayFlag.BLANK, DisplayFlag.LEAD_ERROR):
            result = decode_display(normal_body(flag))
            self.assertFalse(result["primary"]["valid"])
            self.assertIsNone(result["primary"]["value"])

    def test_all_optional_normal_layouts(self):
        for bits in range(16):
            flags = DisplayFlag(bits) | DisplayFlag.AUTO_RANGE
            body = normal_body(flags)
            result = decode_display(body)
            self.assertEqual(len(result["auxiliary"]), (bits & 7).bit_count())
            self.assertEqual("bargraph" in result, bool(bits & 8))
            self.assertEqual(result["tail_hex"], "")
            for length in range(6, len(body)):
                with self.assertRaises(ProtocolError):
                    decode_display(body[:length])

    def test_minmax_layout_and_elapsed_times(self):
        body = struct.pack("<BHHB", 2, int(DisplayFlag.MINMAX), 0x3110, 1)
        body += (
            value(2, with_unit=False)
            + value(5, with_unit=False)
            + struct.pack("<I", 20)
        )
        body += value(3, with_unit=False) + struct.pack("<I", 10)
        body += (
            value(1, with_unit=False)
            + struct.pack("<I", 2)
            + unit()
            + struct.pack("<I", timestamp())
        )
        self.assertEqual(len(body), 50)
        result = decode_display(body)
        self.assertEqual(result["layout"], "minmax")
        self.assertEqual(result["maximum"]["value"], 5)
        self.assertEqual(result["average"]["value"], 3)
        self.assertEqual(result["minimum_elapsed_seconds"], 2)
        self.assertEqual(result["start_time"]["iso"], "2026-11-07T21:37:55")

    def test_comparator_with_without_bargraph(self):
        for bar in [False, True]:
            flags = DisplayFlag.COMPARATOR | (DisplayFlag.BARGRAPH if bar else 0)
            body = normal_body(flags) + bytes([0, 1, 3]) + struct.pack("<ff", 1.0, 2.0)
            result = decode_display(body)
            self.assertEqual(result["layout"], "comparator")
            self.assertEqual(result["comparator"]["result"], "fail")
            self.assertEqual(result["comparator"]["value2"], 2.0)
            self.assertEqual(result["tail_hex"], "")

    def test_recording_layout(self):
        body = normal_body(DisplayFlag.RECORDING) + struct.pack(
            "<IIII", 10, 50, 10, timestamp()
        )
        result = decode_display(body)
        self.assertEqual(result["layout"], "recording")
        self.assertEqual(result["sample_count"], 10)
        self.assertEqual(result["remaining_seconds"], 50)
        self.assertEqual(result["tail_hex"], "")

    def test_saved_reading_date_insertion(self):
        live = normal_body(DisplayFlag(0))
        saved = (
            bytes([Response.SAVED_READING]) + struct.pack("<I", timestamp()) + live[1:]
        )
        result = decode_packet(unpack_frame(frame_body(saved)))
        self.assertEqual(result["kind"], "saved")
        self.assertEqual(result["timestamp"]["day"], 7)
        self.assertEqual(result["primary"]["value"], 1.25)

    def test_record_header(self):
        body = (
            b"\x04"
            + b"LAB".ljust(11, b"\0")
            + unit("k~")
            + struct.pack("<HII", 2, 60, 3)
        )
        body += (
            value(9, with_unit=False)
            + value(1, with_unit=False)
            + value(5, with_unit=False)
            + struct.pack("<I", timestamp())
        )
        result = decode_packet(unpack_frame(frame_body(body)))
        self.assertEqual(result["kind"], "record_header")
        self.assertEqual(result["name"], "LAB")
        self.assertEqual(result["display_unit"], "k\u03a9")
        self.assertEqual(result["duration_seconds"], 60)
        self.assertEqual(result["average"]["value"], 5)

    def test_record_data_and_count_length(self):
        body = b"\x05\x02" + value(1, with_unit=False) + struct.pack("<I", timestamp())
        body += value(2, with_unit=False) + struct.pack("<I", timestamp(second=56))
        result = decode_packet(unpack_frame(frame_body(body)))
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["samples"][1]["value"], 2)
        with self.assertRaises(ProtocolError):
            decode_packet(unpack_frame(frame_body(body[:-1])))

    def test_checksum_valid_er_is_rejection(self):
        result = decode_packet(
            unpack_frame(bytes.fromhex("ab cd 05 00 01 45 52 9d 00"))
        )
        self.assertEqual(result["token"], "ER")
        self.assertFalse(result["accepted"])

    def test_query_response_and_unknown_packet(self):
        response = decode_packet(
            unpack_frame(pack_command(Response.QUERY_RESULT, struct.pack("<BH", 8, 22)))
        )
        self.assertEqual(response["query_command"], Command.QUERY_SAVED_COUNT)
        self.assertEqual(response["count"], 22)
        result = decode_packet(unpack_frame(pack_command(0xE0, b"unknown")))
        self.assertEqual(result["kind"], "unknown")
        self.assertEqual(result["payload_hex"], b"unknown".hex())

    def test_parser_fragmentation_noise_and_checksum(self):
        raw = frame_body(normal_body())
        bad = bytearray(raw)
        bad[-1] ^= 1
        stream = b"noise" + bad + raw
        for seed in range(20):
            rng = random.Random(seed)
            parser = FrameParser()
            packets = []
            i = 0
            while i < len(stream):
                size = rng.randrange(1, 32)
                packets += parser.feed(stream[i : i + size])
                i += size
            self.assertEqual([p.raw for p in packets], [raw])
            self.assertEqual(parser.bad_checksums, 1)
            self.assertEqual(parser.buffer, b"")
        with self.assertRaises(ProtocolError):
            unpack_frame(raw + b"\0")

    def test_invalid_length_bounded(self):
        parser = FrameParser()
        self.assertEqual(parser.feed(b"\xab\xcd\xff\xff"), [])
        self.assertEqual(parser.bad_lengths, 1)
        self.assertLess(len(parser.buffer), 4)

    def test_status_signed_and_nonfinite(self):
        self.assertEqual(decode_value(value(-1.25), 0, unit())["value"], -1.25)
        for status in range(1, 16):
            result = decode_value(value(status=status), 0, unit())
            self.assertFalse(result["valid"])
            self.assertIsNone(result["value"])
        self.assertIsNone(decode_value(value(float("nan")), 0)["value"])
        self.assertIsNone(decode_value(value(float("inf")), 0)["value"])

    def test_timestamp_bounds_and_invalid(self):
        self.assertEqual(decode_timestamp(timestamp(year=2063))["year"], 2063)
        self.assertFalse(decode_timestamp(0)["valid"])

    def test_all_command_payloads(self):
        self.assertEqual(
            pack_command(Command.SET_LIVE_STREAM, b"\1").hex(), "abcd040005010a00"
        )
        self.assertEqual(function_payload(Function.VDC), bytes.fromhex("1031"))
        self.assertEqual(function_payload(Function.VAC, 6), bytes.fromhex("6011"))
        self.assertEqual(range_payload(None), b"\0")
        self.assertEqual(relative_payload(True, 1.25), b"\1" + struct.pack("<f", 1.25))
        self.assertEqual(record_data_payload(2, 123456), struct.pack("<HI", 2, 123456))
        self.assertEqual(
            start_recording_payload("LAB", 1, 60),
            b"LAB".ljust(11, b"\0") + struct.pack("<HI", 1, 60),
        )
        for call in [
            lambda: function_payload(Function.VDC, 9),
            lambda: index_payload(0),
            lambda: index_payload(65535),
            lambda: range_payload(256),
            lambda: relative_payload(True, float("nan")),
            lambda: start_recording_payload("TOO_LONG_NAME", 1, 1),
            lambda: record_data_payload(1, 0),
        ]:
            with self.assertRaises(ValueError):
                call()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeTransport:
    def __init__(self, clock, script):
        self.clock = clock
        self.script = script
        self.rx = deque()
        self.writes = []

    def write(self, raw, timeout):
        packet = unpack_frame(raw)
        self.writes.append(packet)
        for reply in self.script(packet):
            self.rx.append(reply)

    def read(self, timeout):
        if self.rx:
            self.clock.now += 0.001
            return self.rx.popleft()
        self.clock.now += timeout
        return b""


class SessionTests(unittest.TestCase):
    def test_offline_cli_without_usb_dependency(self):
        program = """
import sys
class NoHid:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ("hid", "hidraw"):
            raise ImportError("optional import unavailable")
sys.meta_path.insert(0, NoHid())
from ut8804e import Cp2110Transport, TransportError
from ut8804e.cli import main
try:
    Cp2110Transport().open()
except TransportError as error:
    assert "hidapi" in str(error)
else:
    raise AssertionError("missing backend accepted")
raise SystemExit(main(["functions"]))
"""
        result = subprocess.run(
            [sys.executable, "-c", program],
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        self.assertEqual(
            len([json.loads(line) for line in result.stdout.splitlines()]), 20
        )

    def test_nonfinite_waits_rejected_without_transport_access(self):
        clock = FakeClock()
        transport = FakeTransport(clock, lambda p: [])
        for invalid in (float("nan"), float("inf"), -float("inf"), 0, -1, True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    Meter(transport, timeout=invalid)
                meter = Meter(transport, clock=clock)
                with self.assertRaises(ValueError):
                    meter.read(invalid)
                with self.assertRaises(ValueError):
                    next(meter.readings(seconds=invalid))
        self.assertEqual(transport.writes, [])

    def test_cli_invalid_waits_rejected_before_device_open(self):
        with (
            patch("ut8804e.cli.Cp2110Transport") as bridge,
            redirect_stderr(io.StringIO()),
        ):
            for args in (
                ["--timeout", "nan", "read"],
                ["--timeout", "inf", "read"],
                ["read", "--seconds", "nan"],
                ["read", "--seconds", "inf"],
            ):
                with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)
            bridge.assert_not_called()

    def test_serialized_er_raises_once_no_retry(self):
        clock = FakeClock()
        transport = FakeTransport(clock, lambda p: [pack_command(Response.ACK, b"ER")])
        meter = Meter(transport, clock=clock)
        with self.assertRaises(MeterRejected):
            meter.select_function(Function.VDC)
        self.assertEqual(len(transport.writes), 1)

    def test_function_requires_ok_and_matching_fresh_state(self):
        def script(packet):
            if packet.command == Command.SET_FUNCTION:
                return [
                    pack_command(Response.ACK, b"OK"),
                    frame_body(
                        normal_body(selector=int.from_bytes(packet.payload, "little"))
                    ),
                ]
            return []

        clock = FakeClock()
        transport = FakeTransport(clock, script)
        meter = Meter(transport, clock=clock)
        result = meter.select_function(Function.FREQUENCY)
        self.assertEqual(result["state"]["function_selector"], 0x7110)

    def test_mismatched_function_times_out(self):
        clock = FakeClock()
        transport = FakeTransport(
            clock,
            lambda p: (
                [pack_command(Response.ACK, b"OK"), frame_body(normal_body())]
                if p.command == 1
                else []
            ),
        )
        meter = Meter(transport, clock=clock)
        with self.assertRaises(MeterTimeout):
            meter.select_function(Function.FREQUENCY)

    def test_toggle_has_no_retry_after_lost_ack(self):
        clock = FakeClock()
        transport = FakeTransport(clock, lambda p: [])
        meter = Meter(transport, clock=clock)
        with self.assertRaises(MeterTimeout):
            meter.toggle_hold()
        self.assertEqual(
            [(p.command, p.payload) for p in transport.writes],
            [(Command.TOGGLE_HOLD, b"\x5a")],
        )

    def test_query_matches_its_original_command(self):
        def script(packet):
            return [
                pack_command(Response.QUERY_RESULT, struct.pack("<BH", 14, 9)),
                pack_command(Response.QUERY_RESULT, struct.pack("<BH", 8, 4)),
            ]

        clock = FakeClock()
        transport = FakeTransport(clock, script)
        meter = Meter(transport, clock=clock)
        self.assertEqual(meter.saved_count()["count"], 4)

    def test_storage_writes_require_opt_in(self):
        clock = FakeClock()
        transport = FakeTransport(clock, lambda p: [])
        meter = Meter(transport, clock=clock)
        for call in [
            meter.save_reading,
            lambda: meter.delete_saved(None),
            lambda: meter.delete_record(1),
            lambda: meter.start_recording("LAB", 1, 1),
            meter.stop_recording,
        ]:
            with self.assertRaises(StorageWriteNotEnabled):
                call()
        self.assertEqual(transport.writes, [])

    def test_every_storage_control_emits_exact_opcode(self):
        clock = FakeClock()
        transport = FakeTransport(clock, lambda p: [pack_command(Response.ACK, b"OK")])
        meter = Meter(transport, clock=clock, allow_storage_writes=True)
        meter.save_reading()
        meter.delete_saved(None)
        meter.delete_record(2)
        meter.start_recording("LAB", 1, 1)
        meter.stop_recording()
        self.assertEqual([p.command for p in transport.writes], [6, 9, 15, 10, 11])
        self.assertEqual(transport.writes[1].payload, b"\xff\xff")


class HidHandleMock:
    def __init__(self, backend):
        self.backend = backend
        self.path = None
        self.closed = False

    def open_path(self, path):
        if self.backend.open_error:
            raise OSError("device busy")
        self.path = path
        self.backend.opened.append(path)

    def set_nonblocking(self, value):
        self.backend.nonblocking.append(value)
        return 0

    def get_feature_report(self, feature, length):
        self.backend.gets.append((feature, length))
        if self.backend.short_feature == feature:
            return [feature]
        if self.backend.bad_id == feature:
            return [0] * length
        if feature == Feature.PART_NUMBER:
            return [feature, self.backend.part, 1]
        if feature == Feature.UART_STATUS:
            return list(bytes([feature]) + bytes.fromhex("001200340102"))
        return list(self.backend.state[feature])

    def send_feature_report(self, report):
        report = bytes(report)
        self.backend.features.append(report)
        if (
            self.backend.fail_restore
            and report == self.backend.original[Feature.UART_CONFIG]
        ):
            raise OSError("mock restore failed")
        if self.backend.short_feature_write:
            return len(report) - 1
        self.backend.state[report[0]] = report
        return len(report)

    def write(self, raw):
        self.backend.writes.append(bytes(raw))
        if self.backend.write_error:
            raise OSError("disconnected")
        return len(raw) - 1 if self.backend.short_write else len(raw)

    def read(self, length, timeout_ms):
        self.backend.read_calls.append((length, timeout_ms))
        result = self.backend.reports.popleft() if self.backend.reports else []
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True
        self.backend.closed += 1


class HidApiMock:
    def __init__(self, path=b"device-path"):
        self.state = {
            Feature.UART_ENABLE: b"\x41\0",
            Feature.UART_CONFIG: bytes.fromhex("500001c20000000300"),
        }
        self.original = dict(self.state)
        self.entries = [
            dict(
                path=path,
                vendor_id=0x10C4,
                product_id=0xEA80,
                serial_number="SERIAL_A",
                manufacturer_string="Example",
                product_string="USB bridge",
            )
        ]
        self.enumerations = []
        self.opened = []
        self.nonblocking = []
        self.gets = []
        self.features = []
        self.writes = []
        self.read_calls = []
        self.reports = deque()
        self.closed = 0
        self.fail_restore = False
        self.short_feature = None
        self.bad_id = None
        self.short_feature_write = False
        self.short_write = False
        self.write_error = False
        self.open_error = False
        self.part = 0x0A

    def enumerate(self, vid, pid):
        self.enumerations.append((vid, pid))
        return self.entries

    def device(self):
        return HidHandleMock(self)


class TransportTests(unittest.TestCase):
    def test_portable_path_and_serial_discovery(self):
        paths = (
            b"/dev/hidraw-test",
            b"DevSrvsID:example",
            b"\\\\?\\hid#example",
            b"opaque-\xff",
        )
        for path in paths:
            with self.subTest(path=path):
                backend = HidApiMock(path)
                with patch("ut8804e.transport.hid", backend):
                    found = discover_devices()
                    self.assertEqual(found[0].serial, "SERIAL_A")
                    self.assertEqual(backend.opened, [])
                    with Cp2110Transport(found[0].path, serial="SERIAL_A") as bridge:
                        self.assertEqual(backend.opened, [path])
                    self.assertTrue(bridge.restore_verified)
                self.assertEqual(backend.enumerations, [(0x10C4, 0xEA80)] * 2)

    def test_missing_or_ambiguous_discovery_never_opens(self):
        backend = HidApiMock()
        with patch("ut8804e.transport.hid", backend):
            with self.assertRaises(TransportError):
                Cp2110Transport(serial="UNKNOWN").open()
            backend.entries.append(
                dict(backend.entries[0], path=b"second", serial_number="SERIAL_B")
            )
            with self.assertRaises(TransportError):
                Cp2110Transport().open()
            with self.assertRaises(TransportError):
                Cp2110Transport("device-path", serial="SERIAL_B").open()
            self.assertEqual(backend.opened, [])
            with Cp2110Transport(serial="SERIAL_B"):
                self.assertEqual(backend.opened, [b"second"])

    def test_exact_config_numbered_output_and_restore(self):
        backend = HidApiMock()
        with patch("ut8804e.transport.hid", backend):
            with Cp2110Transport() as bridge:
                self.assertEqual(
                    backend.state[Feature.UART_CONFIG],
                    bytes.fromhex("500000258000000300"),
                )
                self.assertEqual(backend.nonblocking, [1])
                bridge.write(bytes(range(128)))
                self.assertEqual([r[0] for r in backend.writes], [63, 63, 2])
                self.assertTrue(all(len(r) == 64 for r in backend.writes))
                self.assertEqual(backend.writes[0][1:], bytes(range(63)))
                self.assertEqual(bridge.status()["tx_fifo_bytes"], 18)
                self.assertEqual(bridge.status()["rx_fifo_bytes"], 52)
            self.assertTrue(bridge.restore_verified)
        self.assertEqual(backend.state, backend.original)
        self.assertEqual(backend.closed, 1)

    def test_read_timeout_nonblocking_and_numbered_input(self):
        backend = HidApiMock()
        backend.reports.extend(([3, 0xAB, 0xCD, 0x12], [], [1, 0x01]))
        with patch("ut8804e.transport.hid", backend), Cp2110Transport() as bridge:
            self.assertEqual(bridge.read(0.1231), bytes.fromhex("abcd12"))
            self.assertEqual(bridge.read(0.001), b"")
            self.assertEqual(bridge.read(0), b"\x01")
        self.assertEqual(backend.read_calls, [(64, 124), (64, 1), (64, 0)])

    def test_disconnect_and_bad_reports(self):
        backend = HidApiMock()
        backend.reports.extend((OSError("disconnected"), [64] + [0] * 63, [3, 1]))
        with patch("ut8804e.transport.hid", backend), Cp2110Transport() as bridge:
            for _ in range(3):
                with self.assertRaises(TransportError):
                    bridge.read(0.1)
            self.assertEqual(bridge.malformed_reports, 2)
            backend.short_write = True
            with self.assertRaises(TransportError):
                bridge.write(b"request")
            backend.short_write = False
            backend.write_error = True
            with self.assertRaises(TransportError):
                bridge.write(b"request")

    def test_client_error_still_restores(self):
        backend = HidApiMock()
        with patch("ut8804e.transport.hid", backend):
            with self.assertRaisesRegex(RuntimeError, "client"):
                with Cp2110Transport():
                    raise RuntimeError("client")
        self.assertEqual(backend.state, backend.original)
        self.assertEqual(backend.closed, 1)

    def test_failed_config_restore_still_restores_enable_and_closes(self):
        backend = HidApiMock()
        with patch("ut8804e.transport.hid", backend):
            with self.assertRaises(TransportError):
                with Cp2110Transport() as bridge:
                    backend.fail_restore = True
        self.assertFalse(bridge.restore_verified)
        self.assertEqual(
            backend.state[Feature.UART_ENABLE], backend.original[Feature.UART_ENABLE]
        )
        self.assertEqual(backend.closed, 1)

    def test_invalid_feature_or_part_releases_handle(self):
        for field, value in (
            ("short_feature", Feature.UART_CONFIG),
            ("bad_id", Feature.PART_NUMBER),
            ("part", 0),
        ):
            backend = HidApiMock()
            setattr(backend, field, value)
            with patch("ut8804e.transport.hid", backend):
                with self.assertRaises(TransportError):
                    Cp2110Transport().open()
                self.assertEqual(backend.closed, 1)
                setattr(backend, field, None if field != "part" else 0x0A)
                with Cp2110Transport():
                    pass

    def test_open_failure_and_duplicate_owner(self):
        backend = HidApiMock()
        backend.open_error = True
        with patch("ut8804e.transport.hid", backend):
            with self.assertRaises(OSError):
                Cp2110Transport().open()
            backend.open_error = False
            with Cp2110Transport():
                with self.assertRaises(TransportError):
                    Cp2110Transport().open()
            with Cp2110Transport():
                pass

    def test_write_deadline_and_finite_timeout_validation(self):
        backend = HidApiMock()
        with patch("ut8804e.transport.hid", backend), Cp2110Transport() as bridge:
            with patch("ut8804e.transport.time.monotonic", side_effect=[0, 2]):
                with self.assertRaises(TransportTimeout):
                    bridge.write(b"request", 1)
            for invalid in (float("nan"), float("inf"), -1):
                with self.assertRaises(ValueError):
                    bridge.read(invalid)
                with self.assertRaises(ValueError):
                    bridge.write(b"request", invalid)
        self.assertEqual(backend.writes, [])


if __name__ == "__main__":
    unittest.main()
