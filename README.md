# ruvion-client

A Python client for the Ruvion motion controller (QUIC + mTLS).

## Getting started

Clone with submodules:

```bash
git clone --recurse-submodules <repo-url>
# or, if already cloned:
git submodule update --init --recursive
```

Install prerequisites:

- Python ≥ 3.10
- [FlatBuffers compiler](https://github.com/google/flatbuffers) (`flatc`)
  - macOS: `brew install flatbuffers`
  - Ubuntu/Debian: `sudo apt install flatbuffers-compiler`
  - Windows: `winget install Google.FlatBuffers`

Run an example:

```bash
./run.sh
```

On first run, `run.sh` creates `.venv/`, installs the package in editable mode,
generates the FlatBuffers bindings, and copies
[examples/config.example.py](ruvion-client/examples/config.example.py) to
`examples/config.py`. Edit `CERT_DIR` in that file to point at your certificate
directory (`ca.crt`, `client.crt`, `client.key`), then run `./run.sh` again to
pick an example.

## Examples

| Script | Purpose |
| --- | --- |
| `monitor.py` | Live read-only telemetry view (no claim) |
| `streaming_test.py` | Stream a sine wave on joint 0 at 100 Hz |
| `move_test.py` | Drive each joint to a target angle, wait for idle, return to 0 |
| `zeroing_joint7.py` | Zero joint 7 |
| `gravity_comp.py` | Run gravity compensation calibration |
| `friction_comp.py` | Run friction compensation calibration |

## Project layout

```
ruvion-client/                 package sources
submodules/motion-control-protocol/   FlatBuffers schemas (git submodule)
run.sh                         setup + example launcher
```
