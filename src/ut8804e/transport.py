"""Portable CP2110 USB transport using HIDAPI on Windows, macOS and Linux.

Only volatile UART settings are written. Bridge programming, reset, break and
latch writes are outside this transport.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass
from enum import IntEnum

try:
    # The HIDAPI hidraw binding honors Linux HID-node permissions. Other
    # platforms, or builds without it, use the same API through the hid binding.
    import hidraw as hid
except ImportError:
    try:
        import hid
    except ImportError:
        hid = None

REPORT_BYTES = 64
PAYLOAD_BYTES = REPORT_BYTES - 1
CP2110_VID = 0x10C4
CP2110_PID = 0xEA80
_OPEN_PATHS: set[bytes] = set()
_OPEN_PATHS_LOCK = threading.Lock()


def validate_timeout(value, *, allow_zero=False) -> float:
    """Reject nonfinite or unbounded waits before any instrument access."""
    if isinstance(value, bool):
        raise ValueError("Timeout/duration must be a finite number")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError("Timeout/duration must be finite and positive")
    return value


class Feature(IntEnum):
    UART_ENABLE = 0x41
    UART_STATUS = 0x42
    PART_NUMBER = 0x46
    UART_CONFIG = 0x50


class TransportError(OSError):
    """The bridge is unavailable, busy, malformed or cannot be restored."""


class TransportTimeout(TimeoutError):
    """A bounded bridge operation did not complete."""


@dataclass(frozen=True)
class DeviceInfo:
    path: str
    serial: str | None
    manufacturer: str | None
    product: str | None
    vid: int
    pid: int


def _require_hid():
    if hid is None:
        raise TransportError(
            "HIDAPI is unavailable; install the 'hidapi' Python package"
        )
    return hid


def _display_path(path: bytes) -> str:
    try:
        decoded = path.decode("utf-8")
        if not decoded.startswith("hex:"):
            return decoded
    except UnicodeDecodeError:
        pass
    return "hex:" + path.hex()


def _path_bytes(path: str | bytes) -> bytes:
    if isinstance(path, bytes):
        return path
    if path.startswith("hex:"):
        try:
            return bytes.fromhex(path[4:])
        except ValueError as error:
            raise ValueError("Invalid hexadecimal HID device path") from error
    return path.encode("utf-8")


def discover_devices() -> list[DeviceInfo]:
    """List matching bridges without opening or changing any device."""
    backend = _require_hid()
    try:
        entries = backend.enumerate(CP2110_VID, CP2110_PID)
    except OSError as error:
        raise TransportError(f"HID enumeration failed: {error}") from error
    result = {}
    for item in entries:
        if (item.get("vendor_id"), item.get("product_id")) != (CP2110_VID, CP2110_PID):
            continue
        raw_path = item.get("path")
        if not raw_path:
            continue
        path = _display_path(_path_bytes(raw_path))
        result[path] = DeviceInfo(
            path,
            item.get("serial_number"),
            item.get("manufacturer_string"),
            item.get("product_string"),
            CP2110_VID,
            CP2110_PID,
        )
    return [result[path] for path in sorted(result)]


class Cp2110Transport:
    """One serialized owner of a bridge, with exact UART-state restoration.

    A process-local guard rejects duplicate owners in this Python process.
    Other applications/processes must be coordinated separately; no portable
    cross-process exclusive-open guarantee is assumed.
    """

    def __init__(
        self, device: str | bytes | None = None, serial: str | None = None, trace=None
    ):
        self.device = device
        self.serial = serial
        self.trace = trace
        self.handle = None
        self._owned_path = None
        self.original_config = None
        self.original_enable = None
        self.malformed_reports = 0
        self.restore_verified = False
        self.restore_errors = []

    def _feature(self, report: bytes, get=False) -> bytes:
        if self.handle is None:
            raise TransportError("CP2110 is not open")
        report = bytes(report)
        try:
            if get:
                result = bytes(self.handle.get_feature_report(report[0], len(report)))
                if len(result) < len(report) or result[0] != report[0]:
                    raise TransportError(
                        "Truncated or mismatched CP2110 feature report"
                    )
                return result[: len(report)]
            count = self.handle.send_feature_report(report)
            if count != len(report):
                raise TransportError(
                    f"Incomplete CP2110 feature write: {count}/{len(report)}"
                )
            return report
        except (OSError, ValueError) as error:
            raise TransportError(f"CP2110 feature transfer failed: {error}") from error

    def open(self):
        backend = _require_hid()
        if self.handle is not None:
            raise TransportError("CP2110 is already open")
        wanted = None if self.device is None else _path_bytes(self.device)
        matches = [
            d
            for d in discover_devices()
            if (wanted is None or _path_bytes(d.path) == wanted)
            and (self.serial is None or d.serial == self.serial)
        ]
        if len(matches) != 1:
            raise TransportError(
                f"Expected exactly one matching CP2110, found {len(matches)}; specify --device or --serial"
            )
        self.device = matches[0].path
        path = _path_bytes(self.device)
        with _OPEN_PATHS_LOCK:
            if path in _OPEN_PATHS:
                raise TransportError(
                    "This CP2110 is already owned in the current process"
                )
            _OPEN_PATHS.add(path)
            self._owned_path = path
        self.original_config = None
        self.original_enable = None
        self.restore_verified = False
        self.restore_errors = []
        try:
            self.handle = backend.device()
            self.handle.open_path(path)
            # In the Python binding timeout_ms=0 uses the nonblocking read path.
            result = self.handle.set_nonblocking(1)
            if result is not None and result < 0:
                raise TransportError("Could not enable bounded HID reads")
            part = self._feature(bytes([Feature.PART_NUMBER, 0, 0]), True)
            if part[1] != 0x0A:
                raise TransportError("Device does not report CP2110 part number 0x0A")
            self.original_enable = self._feature(bytes([Feature.UART_ENABLE, 0]), True)
            self.original_config = self._feature(
                bytes([Feature.UART_CONFIG]) + bytes(8), True
            )
            self._feature(bytes([Feature.UART_ENABLE, 1]))
            active_config = (
                bytes([Feature.UART_CONFIG])
                + struct.pack(">I", 9600)
                + bytes([0, 0, 3, 0])
            )
            self._feature(active_config)
            if (
                self._feature(bytes([Feature.UART_CONFIG]) + bytes(8), True)
                != active_config
            ):
                raise TransportError(
                    "UART 9600 8N1 configuration did not read back exactly"
                )
            self._emit("open", device=self.device, part_hex=part.hex())
        except BaseException:
            self.close()
            raise
        return self

    def _emit(self, kind, **fields):
        if self.trace is not None:
            self.trace({"kind": kind, "monotonic": time.monotonic(), **fields})

    def write(self, payload: bytes, timeout: float = 2.0):
        """Write complete reports; native HID calls use the OS/backend timeout.

        The deadline bounds progression between reports. HIDAPI's synchronous
        feature/output calls cannot be interrupted by this Python timeout.
        """
        timeout = validate_timeout(timeout)
        if self.handle is None:
            raise TransportError("CP2110 is not open")
        deadline = time.monotonic() + timeout
        for start in range(0, len(payload), PAYLOAD_BYTES):
            if time.monotonic() >= deadline:
                raise TransportTimeout("CP2110 write timed out")
            chunk = bytes(payload[start : start + PAYLOAD_BYTES])
            # The count is the CP2110 numbered Report ID: do not prepend zero.
            report = bytes([len(chunk)]) + chunk + bytes(PAYLOAD_BYTES - len(chunk))
            try:
                count = self.handle.write(report)
            except (OSError, ValueError) as error:
                raise TransportError(f"CP2110 write failed: {error}") from error
            if count != len(report):
                raise TransportError(
                    f"Incomplete atomic HID write: {count}/{len(report)}"
                )
            self._emit("write", report_hex=report.hex(), uart_hex=chunk.hex())

    def read(self, timeout: float = 1.0) -> bytes:
        timeout = validate_timeout(timeout, allow_zero=True)
        if self.handle is None:
            raise TransportError("CP2110 is not open")
        milliseconds = min(math.ceil(min(timeout, 2_147_483.647) * 1000), 2_147_483_647)
        try:
            report = bytes(self.handle.read(REPORT_BYTES, milliseconds))
        except (OSError, ValueError) as error:
            raise TransportError(
                f"CP2110 read failed or device disconnected: {error}"
            ) from error
        if not report:
            return b""  # HIDAPI empty list means timeout, not a measured zero.
        self._emit("read", report_hex=report.hex())
        length = report[0]
        if length > PAYLOAD_BYTES or length + 1 > len(report):
            self.malformed_reports += 1
            raise TransportError(
                f"Malformed CP2110 input report length {length}/{len(report)}"
            )
        return report[1 : length + 1]

    def status(self) -> dict:
        report = self._feature(bytes([Feature.UART_STATUS]) + bytes(6), True)
        return {
            "raw_hex": report.hex(),
            "tx_fifo_bytes": int.from_bytes(report[1:3], "big"),
            "rx_fifo_bytes": int.from_bytes(report[3:5], "big"),
            "error_flags_raw": report[5],
            "line_break_raw": report[6],
        }

    def close(self):
        if self.handle is None and self._owned_path is None:
            return
        self.restore_errors = []
        try:
            if self.handle is not None:
                for name, original in (
                    ("config", self.original_config),
                    ("enable", self.original_enable),
                ):
                    if original is None:
                        continue
                    try:
                        self._feature(original)
                        readback = self._feature(
                            bytes([original[0]]) + bytes(len(original) - 1), True
                        )
                        if readback != original:
                            raise TransportError(
                                f"Original UART {name} did not read back exactly"
                            )
                    except OSError as error:
                        self.restore_errors.append(f"{name}: {error}")
                self.restore_verified = (
                    self.original_enable is not None
                    and self.original_config is not None
                    and not self.restore_errors
                )
        finally:
            try:
                if self.handle is not None:
                    self.handle.close()
            except (OSError, ValueError) as error:
                self.restore_errors.append(f"close: {error}")
                self.restore_verified = False
            finally:
                self.handle = None
                with _OPEN_PATHS_LOCK:
                    _OPEN_PATHS.discard(self._owned_path)
                self._owned_path = None
        self._emit(
            "close",
            restore_verified=self.restore_verified,
            restore_errors=self.restore_errors,
        )

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        if self.restore_errors:
            error = TransportError("; ".join(self.restore_errors))
            if exc is None:
                raise error
            if hasattr(exc, "add_note"):
                exc.add_note(f"CP2110 restoration also failed: {error}")
        return False
