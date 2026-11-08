"""Packet framing, measurement decoding and command encoding for the UT8804E.

This module is pure: it never opens a device or sends a command.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, IntFlag

SYNC = b"\xab\xcd"
MAX_FRAME_BODY = 4096
UNIT_BYTES = 8
ALL_RECORDS = 0xFFFF


class ProtocolError(ValueError):
    """A packet violates a length, checksum or field constraint."""


class Command(IntEnum):
    SET_FUNCTION = 0x01
    SET_RANGE = 0x02
    SET_RELATIVE = 0x03
    SET_MINMAX = 0x04
    SET_LIVE_STREAM = 0x05
    SAVE_READING = 0x06
    READ_SAVED_READING = 0x07
    QUERY_SAVED_COUNT = 0x08
    DELETE_SAVED_READING = 0x09
    START_RECORDING = 0x0A
    STOP_RECORDING = 0x0B
    READ_RECORD_HEADER = 0x0C
    READ_RECORD_DATA = 0x0D
    QUERY_RECORD_COUNT = 0x0E
    DELETE_RECORD = 0x0F
    TOGGLE_HOLD = 0x12


class Response(IntEnum):
    ACK = 0x01
    LIVE_READING = 0x02
    SAVED_READING = 0x03
    RECORD_HEADER = 0x04
    RECORD_DATA = 0x05
    QUERY_RESULT = 0x72


class Function(IntEnum):
    VAC = 0x1100
    MVAC = 0x2100
    VDC = 0x3100
    MVDC = 0x4100
    RESISTANCE = 0x5100
    CONTINUITY = 0x5200
    CONDUCTANCE = 0x5300
    DIODE = 0x6100
    CAPACITANCE = 0x6200
    FREQUENCY = 0x7100
    DUTY_CYCLE = 0x7200
    PULSE_WIDTH = 0x7300
    CELSIUS = 0x8100
    FAHRENHEIT = 0x8200
    UADC = 0x9100
    UAAC = 0x9200
    MADC = 0xA100
    MAAC = 0xA200
    ADC = 0xB100
    AAC = 0xB200


# Function bases and one-based submode choices. The selector is base + submode * 0x10.
SUBMODES = {
    Function.VAC: ("normal", "frequency", "peak", "low_pass", "dbv", "dbm"),
    Function.MVAC: ("normal", "frequency", "peak", "ac_plus_dc"),
    Function.VDC: ("normal", "peak", "ac_plus_dc"),
    Function.MVDC: ("normal", "peak"),
    Function.CELSIUS: ("t1_t2", "t2_t1", "t1_minus_t2", "t2_minus_t1"),
    Function.FAHRENHEIT: ("t1_t2", "t2_t1", "t1_minus_t2", "t2_minus_t1"),
    Function.UADC: ("normal", "peak", "ac_plus_dc"),
    Function.UAAC: ("normal", "frequency", "peak"),
    Function.MADC: ("normal", "peak", "ac_plus_dc"),
    Function.MAAC: ("normal", "frequency", "peak"),
    Function.ADC: ("normal", "peak", "ac_plus_dc"),
    Function.AAC: ("normal", "frequency", "peak"),
}
for _function in Function:
    SUBMODES.setdefault(_function, ("normal",))


class DisplayFlag(IntFlag):
    AUX1 = 0x0001
    AUX2 = 0x0002
    AUX3 = 0x0004
    BARGRAPH = 0x0008
    RELATIVE = 0x0010
    MINMAX = 0x0020
    PEAK = 0x0040
    HOLD = 0x0080
    AUTO_RANGE = 0x0100
    HIGH_VOLTAGE = 0x0200
    BLANK = 0x0400
    LEAD_ERROR = 0x0800
    COMPARATOR = 0x1000
    RECORDING = 0x2000


class ValueStatus(IntEnum):
    NORMAL = 0
    ABOVE_RANGE = 1
    BELOW_RANGE = 2


def _unsigned(value: int, bits: int, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value < 1 << bits
    ):
        raise ValueError(f"{label} must be an unsigned {bits}-bit integer")
    return value


def pack_command(command: Command | int, payload: bytes = b"") -> bytes:
    code = _unsigned(int(command), 8, "command")
    payload = bytes(payload)
    length = len(payload) + 3
    if length > MAX_FRAME_BODY:
        raise ProtocolError("Command body exceeds the bounded parser capacity")
    covered = struct.pack("<HB", length, code) + payload
    return SYNC + covered + struct.pack("<H", sum(covered) & 0xFFFF)


@dataclass(frozen=True)
class Packet:
    command: int
    payload: bytes
    raw: bytes

    @property
    def body(self) -> bytes:
        return self.raw[4:-2]


def unpack_frame(raw: bytes) -> Packet:
    raw = bytes(raw)
    if len(raw) < 7 or raw[:2] != SYNC:
        raise ProtocolError("Missing UT8804E header or truncated frame")
    length = struct.unpack_from("<H", raw, 2)[0]
    if not 3 <= length <= MAX_FRAME_BODY or len(raw) != length + 4:
        raise ProtocolError("Frame length does not match its bounded declaration")
    if sum(raw[2:-2]) & 0xFFFF != struct.unpack_from("<H", raw, len(raw) - 2)[0]:
        raise ProtocolError("Incorrect additive checksum")
    return Packet(raw[4], raw[5:-2], raw)


class FrameParser:
    """Incremental parser; corrupt bytes are discarded without unbounded growth."""

    def __init__(self):
        self.buffer = bytearray()
        self.bad_checksums = 0
        self.discarded_bytes = 0
        self.bad_lengths = 0

    def feed(self, data: bytes) -> list[Packet]:
        self.buffer.extend(data)
        out = []
        while len(self.buffer) >= 4:
            if self.buffer[:2] != SYNC:
                del self.buffer[0]
                self.discarded_bytes += 1
                continue
            length = struct.unpack_from("<H", self.buffer, 2)[0]
            if not 3 <= length <= MAX_FRAME_BODY:
                self.bad_lengths += 1
                del self.buffer[0]
                self.discarded_bytes += 1
                continue
            total = length + 4
            if len(self.buffer) < total:
                break
            raw = bytes(self.buffer[:total])
            try:
                packet = unpack_frame(raw)
            except ProtocolError:
                self.bad_checksums += 1
                del self.buffer[0]
                self.discarded_bytes += 1
                continue
            del self.buffer[:total]
            out.append(packet)
        return out

    def clear(self):
        """Discard a partial packet explicitly, for example after a timeout."""
        self.discarded_bytes += len(self.buffer)
        self.buffer.clear()


def _require(data: bytes, offset: int, size: int):
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ProtocolError(
            f"Truncated field at body offset {offset}: need {size} bytes"
        )


def _u32(data: bytes, offset: int) -> int:
    _require(data, offset, 4)
    return struct.unpack_from("<I", data, offset)[0]


def decode_unit(raw: bytes) -> dict:
    unit = bytes(raw).split(b"\0", 1)[0]
    text = unit.decode("utf-8", errors="backslashreplace")
    return {
        "unit": text,
        "unit_hex": unit.hex(),
        "display_unit": text.replace("~", "\u03a9").replace("`", "\u00b0"),
    }


def decode_value(data: bytes, offset: int, unit: bytes | None = None) -> dict:
    _require(data, offset, 5)
    raw_float = data[offset : offset + 4]
    number = struct.unpack("<f", raw_float)[0]
    precision_status = data[offset + 4]
    status = precision_status & 15
    valid = status == ValueStatus.NORMAL and math.isfinite(number)
    out = {
        "value": number if valid else None,
        "valid": valid,
        "raw_float_hex": raw_float.hex(),
        "status_code": status,
        "status": {0: "normal", 1: "above_range", 2: "below_range"}.get(
            status, "invalid"
        ),
        "decimal_places": precision_status >> 4,
    }
    if unit is not None:
        out.update(decode_unit(unit))
    return out


def decode_timestamp(word: int) -> dict:
    word = _unsigned(word, 32, "timestamp")
    fields = {
        "year": 2000 + (word & 63),
        "month": (word >> 6) & 15,
        "day": (word >> 10) & 31,
        "hour": (word >> 15) & 31,
        "minute": (word >> 20) & 63,
        "second": (word >> 26) & 63,
    }
    try:
        iso = datetime(**fields).isoformat()
    except ValueError:
        iso = None
    return {"raw": word, **fields, "iso": iso, "valid": iso is not None}


def decode_display(body: bytes) -> dict:
    """Decode the normal, min/max, comparator and recording display layouts.

    Input includes the command byte and excludes framing and checksum. Unknown
    tail bytes are preserved. A short or impossible layout is an explicit error.
    """
    _require(body, 0, 6)
    flags = DisplayFlag(struct.unpack_from("<H", body, 1)[0])
    selector = struct.unpack_from("<H", body, 3)[0]
    base = selector & 0xFF00
    function = Function(base).name if base in Function._value2member_map_ else None
    result = {
        "flags": int(flags),
        "flag_names": [f.name for f in DisplayFlag if flags & f],
        "unknown_flags": int(flags) & ~0x3FFF,
        "function_selector": selector,
        "function_code": base >> 8,
        "function": function,
        "submode": (selector >> 4) & 15,
        "range_code": body[5],
        "raw_body_hex": body.hex(),
    }
    if selector & 0xF000 == 0xC000:
        result.update(
            layout="unavailable_function", primary=None, tail_hex=body[6:].hex()
        )
        return result
    if flags & DisplayFlag.MINMAX:
        _require(body, 0, 50)
        unit = body[38:46]
        result.update(
            layout="minmax",
            primary=decode_value(body, 6, unit),
            maximum=decode_value(body, 11, unit),
            average=decode_value(body, 20, unit),
            minimum=decode_value(body, 29, unit),
            maximum_elapsed_seconds=_u32(body, 16),
            average_elapsed_seconds=_u32(body, 25),
            minimum_elapsed_seconds=_u32(body, 34),
            start_time=decode_timestamp(_u32(body, 46)),
        )
        end = 50
    else:
        _require(body, 0, 19)
        result["primary"] = decode_value(body, 6, body[11:19])
        end = 19
        if flags & DisplayFlag.COMPARATOR:
            result["layout"] = "comparator"
            if flags & DisplayFlag.BARGRAPH:
                result["bargraph"] = _decode_bargraph(body, end)
                end += 12
            _require(body, end, 11)
            result["comparator"] = {
                "mode_raw": body[end],
                "result_code": body[end + 1],
                "result": {0: "pass", 1: "fail"}.get(body[end + 1]),
                "decimal_places": body[end + 2],
                "value1": _finite_float(body, end + 3),
                "value2": _finite_float(body, end + 7),
            }
            end += 11
        elif flags & DisplayFlag.RECORDING:
            _require(body, end, 16)
            result.update(
                layout="recording",
                elapsed_seconds=_u32(body, end),
                remaining_seconds=_u32(body, end + 4),
                sample_count=_u32(body, end + 8),
                start_time=decode_timestamp(_u32(body, end + 12)),
            )
            end += 16
        else:
            result["layout"] = "normal"
            result["auxiliary"] = []
            for flag in (DisplayFlag.AUX1, DisplayFlag.AUX2, DisplayFlag.AUX3):
                if flags & flag:
                    _require(body, end, 13)
                    value = decode_value(body, end, body[end + 5 : end + 13])
                    result["auxiliary"].append({"channel": flag.name, **value})
                    end += 13
            if flags & DisplayFlag.BARGRAPH:
                result["bargraph"] = _decode_bargraph(body, end)
                end += 12
    if flags & (DisplayFlag.BLANK | DisplayFlag.LEAD_ERROR):
        result["primary"].update(
            value=None,
            valid=False,
            status="lead_error" if flags & DisplayFlag.LEAD_ERROR else "blank",
        )
    result["tail_hex"] = body[end:].hex()
    return result


def _finite_float(data: bytes, offset: int) -> float | None:
    _require(data, offset, 4)
    value = struct.unpack_from("<f", data, offset)[0]
    return value if math.isfinite(value) else None


def _decode_bargraph(data: bytes, offset: int) -> dict:
    _require(data, offset, 12)
    return {
        "value": _finite_float(data, offset),
        **decode_unit(data[offset + 4 : offset + 12]),
    }


def decode_packet(packet: Packet) -> dict:
    command = packet.command
    body = packet.body
    result = {"command": command, "raw_frame_hex": packet.raw.hex()}
    if command == Response.LIVE_READING:
        result.update(kind="live", **decode_display(body))
    elif command == Response.SAVED_READING:
        _require(body, 0, 5)
        # The saved reading inserts a packed date immediately after command.
        result.update(
            kind="saved",
            timestamp=decode_timestamp(_u32(body, 1)),
            **decode_display(bytes([command]) + body[5:]),
        )
    elif command == Response.ACK:
        _require(body, 0, 3)
        token = body[1:3]
        result.update(
            kind="ack",
            token=token.decode("ascii", errors="backslashreplace"),
            accepted=True if token == b"OK" else False if token == b"ER" else None,
            tail_hex=body[3:].hex(),
        )
    elif command == Response.QUERY_RESULT:
        _require(body, 0, 4)
        query = body[1]
        result.update(
            kind="query_result",
            query_command=query,
            count=struct.unpack_from("<H", body, 2)[0],
            tail_hex=body[4:].hex(),
        )
    elif command == Response.RECORD_HEADER:
        _require(body, 0, 49)
        unit = body[12:20]
        result.update(
            kind="record_header",
            name=body[1:12]
            .split(b"\0", 1)[0]
            .decode("ascii", errors="backslashreplace"),
            name_hex=body[1:12].hex(),
            **decode_unit(unit),
            interval_seconds=struct.unpack_from("<H", body, 20)[0],
            duration_seconds=_u32(body, 22),
            sample_count=_u32(body, 26),
            maximum=decode_value(body, 30, unit),
            minimum=decode_value(body, 35, unit),
            average=decode_value(body, 40, unit),
            start_time=decode_timestamp(_u32(body, 45)),
            tail_hex=body[49:].hex(),
        )
    elif command == Response.RECORD_DATA:
        _require(body, 0, 2)
        count = body[1]
        _require(body, 2, count * 9)
        samples = []
        for i in range(count):
            offset = 2 + i * 9
            samples.append(
                {
                    **decode_value(body, offset),
                    "timestamp": decode_timestamp(_u32(body, offset + 5)),
                }
            )
        result.update(
            kind="record_data",
            count=count,
            samples=samples,
            tail_hex=body[2 + count * 9 :].hex(),
        )
    else:
        result.update(kind="unknown", payload_hex=packet.payload.hex())
    return result


def function_payload(function: Function, submode: int = 1) -> bytes:
    function = Function(function)
    _unsigned(submode, 4, "submode")
    if not 1 <= submode <= len(SUBMODES[function]):
        raise ValueError(
            f"{function.name} supports submodes1..{len(SUBMODES[function])}"
        )
    return struct.pack("<H", int(function) + submode * 0x10)


def range_payload(index: int | None) -> bytes:
    # None / zero is auto; manual indices are a one-byte field. Exact validity
    # still depends on the selected function and the physical instrument.
    return bytes([0 if index is None else _unsigned(index, 8, "range index")])


def relative_payload(enabled: bool, reference: float = 0.0) -> bytes:
    if not math.isfinite(reference):
        raise ValueError("Relative reference must be finite")
    try:
        payload = struct.pack("<?f", bool(enabled), reference)
    except (OverflowError, struct.error) as error:
        raise ValueError("Relative reference does not fit float32") from error
    return payload


def index_payload(index: int, allow_all: bool = False) -> bytes:
    _unsigned(index, 16, "record index")
    if index == 0 or (index == ALL_RECORDS and not allow_all):
        raise ValueError(
            "Record indices start at1;65535 is the explicit delete-all sentinel"
        )
    return struct.pack("<H", index)


def record_data_payload(index: int, offset: int) -> bytes:
    _unsigned(offset, 32, "sample offset")
    if offset < 1:
        raise ValueError("Recorded sample offsets start at1")
    return index_payload(index) + struct.pack("<I", offset)


def start_recording_payload(
    name: str, interval_seconds: int, duration_minutes: int
) -> bytes:
    encoded = name.encode("ascii")
    if not 1 <= len(encoded) <= 9 or b"\0" in encoded:
        raise ValueError("Recording name must contain1..9 non-NUL ASCII bytes")
    _unsigned(interval_seconds, 16, "recording interval seconds")
    _unsigned(duration_minutes, 32, "recording duration minutes")
    if interval_seconds < 1 or duration_minutes < 1:
        raise ValueError("Recording interval and duration must be positive")
    # Vendor wrapper forces name byte9 to NUL and leaves byte10 undefined.
    # We deliberately zero-fill the entire11-byte name/reserved region.
    return encoded.ljust(11, b"\0") + struct.pack(
        "<HI", interval_seconds, duration_minutes
    )
