# Hardware readout observations — 2026-11-08

These observations concern one physical UT8804E using HIDAPI on Linux. They do
not establish physical Windows/macOS acceptance or comprehensive remote control.
The accompanying fixture contains actual, unmodified meter packets with generic
decoder expectations; it contains no USB serial numbers or connection details.

| Front-panel mode | Live selector (hex) | Primary unit | Example |
|---|---|---|---|
| DC voltage | `0x3110` | VDC | See fixture |
| Frequency | `0x7110` | kHz | 1.00670004 kHz |
| Duty cycle | `0x7210` | % | 54.0299988% |

The selectors include the one-based normal submode (`base + 0x10`). A duty-cycle
packet can also contain a frequency bargraph: always check the **primary**
function and unit before interpreting a number. Percent is not hertz.

A request to select frequency from DC voltage was explicitly rejected with
`ER`, raw acknowledgement `abcd05000145529d00`. It was not blindly retried.
Manual front-panel selection then enabled valid duty/frequency readout. This
one rejection does not establish that every function transition is unsupported.
After any requested change, verify fresh live packets rather than treating a
successful host write as proof that the meter changed function.

Invalid primary readings with status code 3 were observed around range changes.
Their general instrument meaning remains unknown. Preserve the raw status and
exclude these readings from numerical windows; do not substitute zero or label
all status-3 packets as autoranging. The fixture verifies `valid: false` and a
null numerical value.

Use bounded settling and a window of valid, matching-function readings after
opening the stream or changing modes. Host receipt timestamps are not instrument
sampling timestamps and do not prove measurement freshness. No sampling latency
or calibration accuracy is established by these packet-decoding checks.
