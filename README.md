# UNI-T UT8804E USB tools

Read and control a UNI-T UT8804E digital multimeter from Python or a command
line on Windows, macOS or Linux. The USB transport uses the CP2110 HID bridge
through [HIDAPI](https://github.com/trezor/cython-hidapi), installed automatically
as a dependency. No vendor runtime or USB-to-serial driver is needed.

The reusable API supports live measurements, function/range requests, HOLD,
relative and min/max controls, saved readings and recorded files. Hardware
readout has been checked in DC voltage, frequency and duty-cycle modes. Other formats are
covered by synthetic tests. Many
control and storage operations are not yet comprehensively hardware-tested;
a successful host write alone is never proof of a mode change.

## Install

Python 3.10 or newer is required. USB access uses the same HIDAPI backend on
Windows, macOS and Linux.

```sh
python -m venv .venv
. .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install .
ut8804e --help
```

For editable development from a checkout:

```sh
python -m pip install -e .
python -m ut8804e --help
```

This package is independent of UNI-T and is not an official vendor driver.
HIDAPI supports Windows, macOS and Linux. This package's backend is tested with
simulated Windows/macOS/Linux device paths and APIs. Live DC-voltage readout
through the HIDAPI backend has passed on Linux; physical Windows and macOS
validation is still pending.
Installations without a compatible binary wheel may need the HIDAPI build
prerequisites described by that dependency. Offline parsing, CLI help and
function listing remain usable even if USB support cannot be loaded.

## Connect and read

Enable communication/transmission on the meter and leave its measurement screen
visible. Ensure the front-panel function and probe sockets are appropriate for
the connected circuit. Close other meter applications before opening this one.

```sh
ut8804e list
ut8804e read --seconds 10
ut8804e read --count 20
ut8804e status
```

Automatic selection requires exactly one matching USB bridge. With several
bridges, use `--serial SERIAL_FROM_LIST` or `--device PATH_FROM_LIST`. Values above are
placeholders, not fixed identifiers. Copy the opaque path from `list` exactly;
paths differ by operating system. A `hex:` path preserves non-UTF-8 path bytes.
The bridge is a HID device, so no COM/tty serial port is expected. `list`
identifies candidates without opening them.

The HIDAPI `hidraw` binding is preferred when available (normally on Linux);
otherwise the portable `hid` binding is used. Both are provided by the `hidapi`
distribution. A custom libusb-based build may instead require access to the
corresponding USB device node; consult its platform setup instructions.

On Windows, the normal HID driver is used. On macOS, allow USB device access
when prompted by the operating system. On Linux, an access-denied error may
require a suitable udev rule or device permissions. On desktops using
systemd-logind, a local rule can grant active-seat access to this bridge family:

```udev
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea80", TAG+="uaccess"
```

A serial-specific rule is preferable when several CP2110 devices are connected.
Reload local rules and reconnect the meter after changing permissions. Avoid
running a general measurement workflow as root merely to bypass device access.

Output is one JSON object per packet. Numeric readings include validity,
precision, original unit, normalized display unit, function/range, optional
auxiliary/bargraph data, raw frame and host UTC/monotonic receipt timestamps.
Values remain in their transmitted unit: for example `1.25 kHz` is not silently
converted to `1250 Hz`. Blank, lead-error, overrange and nonfinite values are
marked invalid and have `value: null`.

```sh
ut8804e --timeout 3 --trace capture.jsonl read --seconds 30
```

Timeouts and durations must be finite and positive. HID input reads use explicit
millisecond timeouts. Output/feature calls use the operating system's synchronous
HIDAPI transfer timeout; the Python write deadline is checked between reports
and cannot interrupt a native transfer already in progress. A timed read that obtains no
measurement exits with an error. Traces include raw reports and readings; store
or share them according to the sensitivity of the measured circuit.

The tool temporarily selects bridge UART 9600 8N1 and enables it. On close it
restores the bridge's prior UART settings and enable state and checks readback.
This does not change the front-panel measurement function. The protocol does
not provide a reliable query of the prior live-stream state, so that state
cannot be automatically restored. Context managers and CLI interrupt handling
ensure ordinary error and shutdown paths release the device.

## Controls

```sh
ut8804e functions
ut8804e function vdc --submode 1
ut8804e range auto
ut8804e hold on
ut8804e hold off
ut8804e relative on --reference 1.25
ut8804e relative off
ut8804e minmax on
ut8804e minmax off
```

Function selection does not move the physical rotary switch. Some transitions
are rejected with `ER`; use the appropriate front-panel position and inspect the
error rather than assuming the request succeeded. Function and range requests
require both an accepted response and a fresh matching display state. Manual
range indices are function-specific; this library does not invent a universal
index-to-voltage/current mapping.

`hold toggle` is explicitly non-idempotent. The tool serializes commands and
never blindly retries an ambiguous control request. Acknowledgements contain no
transaction ID, so unrelated concurrent control can still make replies
ambiguous. A process-local guard prevents duplicate owners within one Python process.
There is no cross-process exclusive-open guarantee: coordinate other tools and
applications separately.

## Saved readings and recordings

Read-only operations:

```sh
ut8804e saved-count
ut8804e saved-read 1
ut8804e record-count
ut8804e record-header 1
ut8804e record-download 1
```

Storage changes require explicit opt-in:

```sh
ut8804e --allow-storage-writes save
ut8804e --allow-storage-writes record-start sample --interval-seconds 1 --duration-minutes 2
ut8804e --allow-storage-writes record-stop
ut8804e --allow-storage-writes saved-delete 1
ut8804e --allow-storage-writes record-delete 1
```

Deletion also accepts the explicit word `all`. Recording names are limited to
nine ASCII characters. Stored sample downloads are sequential because returned
blocks contain no file index or offset to correlate a delayed response. A
failed download stops instead of guessing or automatically retrying it.

Calibration, factory settings, firmware updates and bridge OTP programming are
outside this API. Supported encodings and their current limits are documented
in [PROTOCOL.md](PROTOCOL.md).

## Python API

```python
from ut8804e import Cp2110Transport, Meter

with Cp2110Transport() as transport:
    meter = Meter(transport, timeout=3.0)
    for packet in meter.readings(count=10):
        reading = packet["primary"]
        if reading is not None and reading["valid"]:
            print(reading["value"], reading["unit"])
```

Specify `Cp2110Transport(serial="SERIAL_FROM_LIST")` when needed. Handle
`MeterRejected`, `MeterTimeout`, `ProtocolError` and `OSError` according to the
operation. The API has no background thread: use one serialized owner.
`allow_storage_writes=True` is required when constructing a `Meter` that may
save/delete readings or start/stop recordings.

See [examples/read_measurements.py](examples/read_measurements.py) for an
executable example. Agents can use [SKILL.md](SKILL.md) for a concise workflow.

## Manufacturer documentation

See [MANUALS.md](MANUALS.md) for official user-manual links, front-panel clock
setting and the applicability caveat for the programming-manual download.

## Development and validation

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

An optional [GitHub Actions template](ci/github-actions.yml) covers Windows,
macOS and Linux with Python 3.10, 3.12 and 3.14. To enable it, place it in
`.github/workflows/` in your repository. The template is provided for automated
validation; its presence alone does not mean those hosted jobs have run.

The test suite uses synthetic packets and a simulated transport. It covers
variable display layouts, checksums, fragmentation, status/units, command
payloads, rejected/missing acknowledgements, finite waits, explicit storage
opt-in and bridge-state restoration. A simulated pass does not establish
physical acceptance of every function, range or storage command. Shorter
no-bargraph and min/max/comparator/recording packets are currently fixture-tested;
hardware checks do not yet cover every layout.

MIT licensed; see [LICENSE](LICENSE).

## Support this project

If you find this project useful, please give it a star on
[GitHub](https://github.com/bezstpav/unit-ut8804e). It helps others discover it.

See [dated hardware readout observations](docs/hardware_observations_2026_11_08.md)
for captured selectors, invalid readings and a rejected function transition.
