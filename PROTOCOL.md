# UT8804E protocol reference

This reference describes the UT8804E packet format used by this library. It is
not a guarantee that every instrument state accepts every listed command.
Unknown flags, extra bytes and raw units are retained rather than assigned
unverified meanings. The scope is this model; protocols for similarly named
meters should not be assumed compatible.

Command identifiers, function bases, flag masks and HID feature identifiers
below use hexadecimal notation. Offsets, sizes, times and counts use decimal
notation.

## USB transport and framing

The portable backend uses HIDAPI to access a CP2110 HID-to-UART bridge, commonly VID `10c4`, PID `ea80`.
The host UART configuration is 9600 baud, eight data bits, no parity and one
stop bit. HID output reports are 64 bytes: a count byte, up to 63 UART bytes and
zero padding. The count byte is the numbered HID Report ID, so an additional zero Report ID
must not be prepended. Input reports also begin with this UART-byte count. A packet may span
several HID reports, and one report can contain parts of multiple packets.

```text
AB CD | length:u16le | command:u8 | payload:N bytes | checksum:u16le
                        <------ length=N+3 ------>
```

The checksum is the additive sum modulo 65536 of both length bytes, the command
and the payload. It excludes `AB CD` and the checksum itself. Total packet size
is `N+7`. Multibyte numeric fields are little endian unless specified otherwise.
The host parser bounds the body to 4096 bytes as a defensive limit; that is not
a claim about the meter's storage or maximum transfer capability.

Starting live output sends:

```text
AB CD 04 00 05 01 0A 00
```

The bridge uses HID feature `0x41` for UART enable, `0x50` for UART configuration, `0x42` for
FIFO/error status and `0x46` for its part number. Bridge configuration uses a
big-endian baud field, independently of the little-endian instrument protocol.
The library saves and restores the bridge's previous configuration and enable
state. It does not reset or program bridge identity, pins or OTP.

## Commands

| Command | Payload | API/CLI operation |
|---|---|---|
|01|function/submode u16le|function|
|02|range u8:0 automatic, otherwise manual index|range|
|03|enable u8 + reference float32le|relative|
|04|0 exit,1 start/restart|minmax|
|05|1 live,0 pause|live/read|
|06|empty|save|
|07|saved-reading index u16le|saved-read|
|08|empty|saved-count|
|09|saved-reading index u16le; FFFF all|saved-delete|
|0A|17-byte recording parameters|record-start|
|0 B|empty|record-stop|
|0C|recorded-file index u16le|record-header|
|0D|file index u16le + sample offset u32le, first=1|record-download|
|0E|empty|record-count|
|0F|recorded-file index u16le; FFFF all|record-delete|
|12|literal `0x5A`, non-idempotent HOLD toggle|hold|

Start-recording parameters: bytes 0..10 contain an ASCII name and reserved
padding; interval u16le seconds at 11; duration u32le **minutes** at 13. This
library limits names to nine ASCII characters and zeroes the reserved region.
The recorded-file header reports duration in **seconds**, not minutes.

Saving, deleting and recording require explicit storage-write opt-in. No
calibration, factory-programming, firmware-update or bridge-programming command
is provided. Lack of such commands in this API is not a statement that the
instrument has no separate service interface.

### Measurement functions and ranges

The selector is the base below plus `submode*0x10`. Submodes are one-based;
`ut8804e functions` lists supported choices. The physical rotary switch may
restrict a requested transition. In particular, requesting a different function
can return `ER`; callers must inspect the response and fresh display state.

| Base | Function | Base | Function |
|---|---|---|---|
|1100|VAC|2100|mVAC|
|3100|VDC|4100|mVDC|
|5100|resistance|5200|continuity|
|5300|conductance|6100|diode|
|6200|capacitance|7100|frequency|
|7200|duty|7300|pulse width|
|8100|Celsius|8200|Fahrenheit|
|9100|uADC|9200|uAAC|
|A100|mADC|A200|mAAC|
|B100|ADC|B200|AAC|

Manual range indices are function-specific. No universal index-to-physical-range
mapping is assumed. A transmitted float is already scaled to its unit.

## Replies

| Command | Meaning |
|---|---|
|01|two-byte ASCII `OK` or `ER` acknowledgement|
|02|live display, variable layout|
|03|saved display with inserted timestamp|
|04|recorded-file header|
|05|recorded-sample block|
|72|query result: originating opcode08/0E, count u16le|

An acknowledgement contains no command or transaction ID. Serialize operations
and do not issue controls from another application concurrently. `ER` is an
explicit rejection. Missing/ambiguous acknowledgements are not automatically
retried, particularly for HOLD, saves or deletions. An `OK` alone does not prove
a new measurement state; function/range controls also wait for matching data.

The offsets below are relative to the **body including the command byte**,
excluding sync, length and checksum. Extra tail bytes and raw frames remain
available in decoded output.

### Display header and reading records

Common header: command at 0, flags u16le at 1, function/submode u16le at 3, range u8
at 5. Each primary/auxiliary reading occupies13 bytes:

| Offset in reading | Meaning |
|---|---|
|0..3|IEEE754 float32le|
|4|high nibble: decimal places; low nibble: status|
|5..12|eight unit bytes|

Status0 means normal,1 above range,2 below range; other values are invalid.
Nonfinite floats, blank and lead-error displays produce `valid:false` and
`value:null`, while the raw float is preserved. Unit`~` is displayed as Ω and
backtick as the degree symbol. Raw units and their bytes are retained.

| Flag | Meaning | Flag | Meaning |
|---|---|---|---|
|0001|aux1|0002|aux2|
|0004|aux3|0008|bargraph|
|0010|relative|0020|min/max|
|0040|peak|0080|hold|
|0100|auto range|0200|high voltage|
|0400|blank|0800|lead error|
|1000|comparator|2000|recording|

Bits14/15 have no assigned meaning. When flags overlap, layout selection is
MINMAX, then COMPARATOR, then RECORDING, otherwise normal.

| Layout | Fields after the six-byte common header |
|---|---|
|normal|13 B primary, flagged13B aux1/2/3, optional12B bargraph(float32+unit8)|
|minmax|current6/10; max11/15 and elapsed16; average20/24 and elapsed25; min29/33 and elapsed34; shared unit38..45; start time46..49|
|comparator|13 B primary, optional12B bargraph,11 B comparator parameters|
|recording|13 B primary; elapsed u32 at 19, remaining23, sample count27, start time31|

Min/max entries use `value/status` offsets. Elapsed values are u32 seconds.
Comparator parameters are mode u8, result u8 (0 PASS/1 FAIL), precision u8 and
two float32 values. Minimum normal packet size is25B, or37B with a bargraph.

Saved command03 inserts a packed timestamp at body1..4, shifting the common
header/data by four bytes. The same display layout decoder then applies.

### Recorded files

Recorded header command04 has a 49-byte body:

| Offset | Field |
|---|---|
|1..11|name/reserved11B|
|12..19|unit8B|
|20|interval u16le seconds|
|22|duration u32le seconds|
|26|sample count u32le|
|30/34|maximum float/status|
|35/39|minimum float/status|
|40/44|average float/status|
|45|packed start timestamp u32le|

Recorded command05 has count u8 at 1, followed by count nine-byte samples at 2:
float32le, precision/status byte, timestamp u32le. It does not echo the requested
file or sample offset. Downloads must be sequential; after an ambiguous timeout,
a late block cannot safely be attributed to a retried request.

Packed timestamps use year bits 0..5 plus2000, month6..9, day10..14, hour15..19,
minute20..25 and second26..31. They have no timezone field. Invalid calendar
values remain raw with `valid:false`; no clock-accuracy claim is made.

## Validation limits

Synthetic tests exercise every listed command encoding and receive layout.
Hardware readout has been checked for DC voltage and frequency.
Other functions, function transitions, manual ranges, storage operations and all
layout combinations have not been comprehensively validated on hardware.
Record these distinctions when reporting results; an emitted packet or accepted
host write is not evidence of a successful instrument operation.


## Portable HIDAPI behavior

The HIDAPI `hidraw` binding is preferred when available; otherwise the `hid`
binding supplies the same interface on Windows, macOS and Linux. Discovery uses
VID/PID and optional serial/path selection. Feature transfers
include the feature ID; interrupt reports include their numbered Report ID.
HIDAPI input calls use a millisecond timeout; zero is explicitly nonblocking.
An empty input list is a timeout, not a zero-valued measurement; I/O errors are
reported as transport failures. Prior UART settings and enable state are
restored and checked on close, including errors and interrupted sessions.

The backend uses the same API on Windows, macOS and Linux. Simulated backend
tests cover enumeration, path/serial selection, report boundaries, errors,
timeouts and restoration. Live DC-voltage readout through this backend has passed on Linux. Physical
Windows/macOS acceptance remains pending.
The synchronous native output/feature transfer itself uses the OS/backend
transfer timeout; Python cannot cancel an already executing native transfer.

HIDAPI report-number and timeout semantics are described in the
[HIDAPI API reference](https://libusb.info/hidapi/group__API.html) and
[Python binding documentation](https://trezor.github.io/cython-hidapi/).
