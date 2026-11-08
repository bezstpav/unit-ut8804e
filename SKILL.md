---
name: ut8804e
description: Read and control a UNI-T UT8804E multimeter through its CP2110 USB HID connection on Windows, macOS or Linux using this Python library and CLI.
---

# UT8804E measurement and control

Use this repository's `ut8804e` CLI or `Meter` API. Start with [README.md](README.md)
for installation/commands; read [PROTOCOL.md](PROTOCOL.md) when interpreting raw
frames, units, status or stored data. Do not substitute a similar meter model's
commands. See [MANUALS.md](MANUALS.md) for manufacturer documents and clock-setting limits.

- Install the package with `python -m pip install .`; HIDAPI is a dependency.
  Run `ut8804e list`, then select a serial or opaque HID path if several bridges exist.
  Reading requires enabled meter communication and appropriate device permissions.
- Keep one serialized USB owner across processes. A competing application can consume replies or
  make acknowledgements ambiguous. Coordinate ownership before starting a reader.
- Use `ut8804e read --seconds 5` or `status` for a bounded check. Preserve units,
  validity and raw frame when reporting measurements; invalid values are not zero.
- The transport temporarily selects 9600 8N1 and restores its previous UART
  configuration/enable state on close. Use its context manager and handle errors;
  do not leave an abandoned reader running.
- Changing a measurement function changes how a connected circuit is measured.
  Confirm the intended function/probe sockets before a control request. The
  physical selector may gate transitions. Treat `ER` as rejection and require a
  fresh matching state, not merely a successful USB write.
- ACKs have no transaction ID. Do not blindly retry an ambiguous HOLD toggle,
  save, delete or other potentially non-idempotent operation.
- Save/delete/record operations require explicit user intent and
  `--allow-storage-writes` or `Meter(..., allow_storage_writes=True)`. Read-only
  counts/downloads do not grant permission to change storage.
- No calibration, factory settings, firmware-update or bridge-programming API is
  provided. Do not expand a normal measurement request into those operations.

Example read-only session:

```sh
ut8804e list
ut8804e read --count 10
```

Example control, after confirming the connected circuit and front-panel mode:

```sh
ut8804e function vdc --submode 1
ut8804e range auto
```

Stop and report an explicit rejection, timeout, unknown status or restoration
failure. Distinguish a synthetic test, received measurement and physically
confirmed control when describing validation.
