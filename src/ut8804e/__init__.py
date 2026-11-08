"""Python API for the UNI-T UT8804E digital multimeter."""

from .meter import Meter, MeterRejected, MeterTimeout, StorageWriteNotEnabled
from .protocol import Command, DisplayFlag, FrameParser, Function, ProtocolError
from .transport import (
    Cp2110Transport,
    TransportError,
    TransportTimeout,
    discover_devices,
)

__version__ = "0.1.0"
__all__ = [
    "Meter",
    "MeterRejected",
    "MeterTimeout",
    "StorageWriteNotEnabled",
    "Command",
    "DisplayFlag",
    "FrameParser",
    "Function",
    "ProtocolError",
    "Cp2110Transport",
    "TransportError",
    "TransportTimeout",
    "discover_devices",
]
