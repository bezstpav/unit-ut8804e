"""Serialized UT8804E measurement and control session."""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone

from .protocol import (
    ALL_RECORDS,
    Command,
    DisplayFlag,
    FrameParser,
    Function,
    ProtocolError,
    decode_packet,
    function_payload,
    index_payload,
    pack_command,
    range_payload,
    record_data_payload,
    relative_payload,
    start_recording_payload,
)
from .transport import (
    validate_timeout,
)


class MeterRejected(RuntimeError):
    """The instrument sent the checksum-valid response01/ASCII ER."""


class MeterTimeout(TimeoutError):
    """A command or reading did not produce its required bounded response."""


class StorageWriteNotEnabled(PermissionError):
    """Storage-changing operations require explicit caller opt-in."""


class Meter:
    """Serialized UT8804E session over a small read/write transport interface.

    A transport supplies read(timeout)->bytes and write(bytes,timeout). Its bytes
    exclude HID length prefixes. Only one owner/thread may issue operations.
    ACKs have no transaction ID; commands are never retried automatically.
    """

    def __init__(
        self,
        transport,
        *,
        timeout=3.0,
        allow_storage_writes=False,
        trace=None,
        clock=time.monotonic,
    ):
        timeout = validate_timeout(timeout)
        self.transport = transport
        self.timeout = timeout
        self.allow_storage_writes = allow_storage_writes
        self.trace = trace
        self.clock = clock
        self.parser = FrameParser()
        self.pending = deque()
        self.last_display = None
        self.streaming = None
        self.invalid_packets = 0

    def _emit(self, event_type, **fields):
        if self.trace:
            self.trace({"event": event_type, "monotonic": self.clock(), **fields})

    def _send(self, command, payload=b""):
        frame = pack_command(command, payload)
        self.transport.write(frame, self.timeout)
        self._emit("command", command=int(command), raw_frame_hex=frame.hex())

    def _next(self, timeout=None):
        if timeout is not None:
            timeout = validate_timeout(timeout)
        deadline = self.clock() + (self.timeout if timeout is None else timeout)
        while True:
            if self.pending:
                packet = self.pending.popleft()
                try:
                    decoded = decode_packet(packet)
                except ProtocolError:
                    self.invalid_packets += 1
                    self._emit("invalid_packet", raw_frame_hex=packet.raw.hex())
                    continue
                if decoded.get("kind") == "live":
                    self.last_display = decoded
                decoded["received_utc"] = datetime.now(timezone.utc).isoformat()
                decoded["received_monotonic"] = self.clock()
                self._emit("packet", **decoded)
                return decoded
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise MeterTimeout("No matching meter response before timeout")
            self.pending.extend(self.parser.feed(self.transport.read(remaining)))

    def _wait(self, predicate, timeout=None):
        if timeout is not None:
            timeout = validate_timeout(timeout)
        deadline = self.clock() + (self.timeout if timeout is None else timeout)
        while self.clock() < deadline:
            response = self._next(deadline - self.clock())
            if response.get("kind") == "ack" and response.get("accepted") is False:
                raise MeterRejected(
                    f"UT8804E rejected the command: {response['raw_frame_hex']}"
                )
            if predicate(response):
                return response
        raise MeterTimeout("The required meter response did not arrive")

    def _drain_stale(self):
        """Consume earlier frames before a serialized request, bounded to150ms."""
        deadline = self.clock() + 0.15
        self.pending.clear()
        while self.clock() < deadline:
            data = self.transport.read(min(0.02, deadline - self.clock()))
            if not data:
                break
            for packet in self.parser.feed(data):
                try:
                    decoded = decode_packet(packet)
                    if decoded.get("kind") == "live":
                        self.last_display = decoded
                except ProtocolError:
                    self.invalid_packets += 1
        self.pending.clear()
        self.parser.clear()

    def _control(self, command, payload=b""):
        self._drain_stale()
        self._send(command, payload)
        response = self._wait(lambda r: r.get("kind") == "ack")
        if response.get("accepted") is not True:
            raise ProtocolError(
                f"Unrecognized acknowledgement token {response.get('token')!r}"
            )
        return response

    def live(self, enabled=True):
        self._send(Command.SET_LIVE_STREAM, bytes([bool(enabled)]))
        self.streaming = bool(enabled)

    def read(self, timeout=None):
        if timeout is not None:
            timeout = validate_timeout(timeout)
        if self.streaming is not True:
            self.live(True)
        return self._wait(lambda r: r.get("kind") == "live", timeout)

    def readings(self, count=None, seconds=None):
        if count is not None and count < 1:
            raise ValueError("Count must be positive")
        if seconds is not None:
            seconds = validate_timeout(seconds)
        deadline = None if seconds is None else self.clock() + seconds
        total = 0
        while count is None or total < count:
            remaining = (
                self.timeout
                if deadline is None
                else min(self.timeout, deadline - self.clock())
            )
            if remaining <= 0:
                return
            try:
                reading = self.read(remaining)
            except MeterTimeout:
                if deadline is not None and self.clock() >= deadline:
                    return
                raise
            total += 1
            yield reading

    def select_function(self, function: Function, submode=1):
        """Request a function/submode; this does not move the physical knob.

        Some function changes can be rejected with ER depending on meter state.
        Even an OK token is followed by a fresh matching state requirement.
        """
        payload = function_payload(function, submode)
        ack = self._control(Command.SET_FUNCTION, payload)
        if self.streaming is not True:
            self.live(True)
        selector = int.from_bytes(payload, "little")
        state = self._wait(
            lambda r: r.get("kind") == "live" and r.get("function_selector") == selector
        )
        return {"ack": ack, "state": state}

    def set_range(self, index: int | None):
        payload = range_payload(index)
        ack = self._control(Command.SET_RANGE, payload)
        if self.streaming is not True:
            self.live(True)
        wanted = payload[0]
        state = self._wait(
            lambda r: (
                r.get("kind") == "live"
                and (
                    bool(r.get("flags", 0) & DisplayFlag.AUTO_RANGE)
                    if wanted == 0
                    else not r.get("flags", 0) & DisplayFlag.AUTO_RANGE
                    and r.get("range_code") == wanted
                )
            )
        )
        return {"ack": ack, "state": state}

    def toggle_hold(self):
        # Non-idempotent: deliberately no retry after a missing ACK.
        return self._control(Command.TOGGLE_HOLD, b"\x5a")

    def set_hold(self, enabled: bool):
        before = self.read()
        if bool(before["flags"] & DisplayFlag.HOLD) == bool(enabled):
            return {"sent": False, "state": before}
        ack = self.toggle_hold()
        state = self._wait(
            lambda r: (
                r.get("kind") == "live"
                and bool(r.get("flags", 0) & DisplayFlag.HOLD) == bool(enabled)
            )
        )
        return {"sent": True, "ack": ack, "state": state}

    def relative(self, enabled: bool, reference=0.0):
        return self._control(Command.SET_RELATIVE, relative_payload(enabled, reference))

    def minmax(self, enabled: bool):
        return self._control(Command.SET_MINMAX, bytes([bool(enabled)]))

    def _storage_write(self):
        if not self.allow_storage_writes:
            raise StorageWriteNotEnabled(
                "Pass allow_storage_writes=True / --allow-storage-writes for this operation"
            )

    def save_reading(self):
        self._storage_write()
        return self._control(Command.SAVE_READING)

    def _query(self, command, payload, predicate):
        self._drain_stale()
        self._send(command, payload)
        return self._wait(predicate)

    def saved_count(self):
        return self._query(
            Command.QUERY_SAVED_COUNT,
            b"",
            lambda r: (
                r.get("kind") == "query_result"
                and r.get("query_command") == Command.QUERY_SAVED_COUNT
            ),
        )

    def record_count(self):
        return self._query(
            Command.QUERY_RECORD_COUNT,
            b"",
            lambda r: (
                r.get("kind") == "query_result"
                and r.get("query_command") == Command.QUERY_RECORD_COUNT
            ),
        )

    def read_saved(self, index: int):
        self.live(False)
        return self._query(
            Command.READ_SAVED_READING,
            index_payload(index),
            lambda r: r.get("kind") == "saved",
        )

    def record_header(self, index: int):
        self.live(False)
        return self._query(
            Command.READ_RECORD_HEADER,
            index_payload(index),
            lambda r: r.get("kind") == "record_header",
        )

    def record_data(self, index: int, offset: int = 1):
        return self._query(
            Command.READ_RECORD_DATA,
            record_data_payload(index, offset),
            lambda r: r.get("kind") == "record_data",
        )

    def download_record(self, index: int, max_samples: int = 1000000):
        """Yield header, then sequential data packets; no ambiguous retransmits.

        Data replies contain neither file index nor sample offset. Do not run
        overlapping reads or silently retry a delayed/missing block.
        """
        header = self.record_header(index)
        total = header["sample_count"]
        if total > max_samples:
            raise ValueError(
                f"Record has{total}samples, above explicit limit{max_samples}"
            )
        yield header
        offset = 1
        while offset <= total:
            block = self.record_data(index, offset)
            count = block["count"]
            if count == 0 or count > total - offset + 1:
                raise ProtocolError("Empty or oversized recorded-data block")
            block["requested_index"] = index
            block["requested_offset"] = offset
            for sample in block["samples"]:
                sample.update(
                    {k: header[k] for k in ("unit", "unit_hex", "display_unit")}
                )
            yield block
            offset += count

    def delete_saved(self, index: int | None):
        self._storage_write()
        return self._control(
            Command.DELETE_SAVED_READING,
            index_payload(
                ALL_RECORDS if index is None else index, allow_all=index is None
            ),
        )

    def delete_record(self, index: int | None):
        self._storage_write()
        return self._control(
            Command.DELETE_RECORD,
            index_payload(
                ALL_RECORDS if index is None else index, allow_all=index is None
            ),
        )

    def start_recording(self, name, interval_seconds, duration_minutes):
        self._storage_write()
        return self._control(
            Command.START_RECORDING,
            start_recording_payload(name, interval_seconds, duration_minutes),
        )

    def stop_recording(self):
        self._storage_write()
        return self._control(Command.STOP_RECORDING)
