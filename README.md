# fips-lab

Physical-device orchestration for testing `Amperstrand/fips` and `Amperstrand/microfips` on a private lab testbed.

This repo is intentionally separate from upstream FIPS. Upstream keeps Docker/CI chaos tests; this lab adds real hardware coverage: BLE hosts, ESP32/microfips boards, routers, old laptops, and later Windows/OpenWrt machines.

## Design

`fips-lab` mirrors the upstream chaos runner shape:

1. Load a YAML scenario.
2. Resolve real devices from an inventory.
3. Generate isolated lab ACL artifacts (`peers.allow`, `peers.deny`).
4. Query FIPS nodes with `fipsctl` through local or SSH transports.
5. Capture per-run provenance, snapshots, and logs under `results/`.

The first priority is not broad CI. It is repeatable physical evidence for a specific git commit and device set.

## Isolation Policy

Lab FIPS nodes should not join the public/broader FIPS network while BLE and microfips testing is underway.

Scenarios use:

```yaml
isolation:
  mode: lab-allowlist
  deny_unknown_peers: true
  write_peers_allow: true
  write_peers_deny_all: true
```

The runner writes:

- `generated-acl/peers.allow` with lab peer npubs when known
- `generated-acl/peers.deny` containing `ALL`

FIPS ACL behavior is allow-first, then deny. So lab peers in `peers.allow` are permitted, and everyone else is denied by `ALL`.

## Quick Start

```bash
make setup
cp inventory/lab.example.yaml inventory/lab.yaml
$EDITOR inventory/lab.yaml
make list
make dry-run-smoke
```

A dry run does not touch devices. It verifies scenario loading, inventory resolution, lab ACL generation, metadata, snapshots, and result directory layout.

## Real Runs

```bash
make test-microfips-smoke
make test-lab-3node
```

`test-microfips-smoke` is the intended default short regression:

- flash one microfips ESP32
- start/query one Linux FIPS host
- wait for BLE peer connection
- run for 5 minutes
- collect FIPS snapshots and serial/BLE capture intent
- report pass/fail artifacts

`test-lab-3node` includes this Mac, the Linux host, and one ESP32 as an isolated lab mesh.

## Inventory

Real device details live in `inventory/lab.yaml`, which is gitignored.

Device classes currently modeled:

- `transport: local` for this Mac
- `transport: ssh` for Linux/OpenWrt hosts
- `transport: serial` for locally attached microcontrollers
- `transport: serial-via-ssh` for boards attached to remote Linux hosts

## Artifacts

The desired long-term flow is:

- `Amperstrand/fips` builds clean FIPS artifacts for a commit.
- `Amperstrand/microfips` builds clean firmware artifacts for a commit.
- `fips-lab` downloads/deploys/flashes those artifacts and records commit provenance.

For v0.1, Linux can still build on target because that is the current working pattern and avoids cross-compilation friction.

## Current Status

This is a bootstrap skeleton. Implemented now:

- CLI runner
- scenario and inventory loading
- local/SSH/serial device abstractions
- dry-run snapshots
- lab allowlist artifact generation
- result metadata and provenance

Next implementation steps:

- SSH deploy/restart helpers for Linux FIPS
- `esptool.py` flashing for microfips
- serial log streaming
- `btmon` capture on Linux
- real assertion evaluation from `show_peers`, `show_mmp`, and serial logs
