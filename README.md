# fips-lab

Physical-device BLE test framework for FIPS and microfips, using labgrid. Runs automated benchmarks and regression tests against real hardware: a MacBook, a Linux box, and ESP32 boards.

## Architecture Overview

Two layers sit on top of labgrid:

**Layer 1: `fips_lab/`** -- custom labgrid drivers and strategy that know how to manage FIPS services, run `fipsctl` commands, flash ESP32s, and track device readiness. These drivers are registered with labgrid's `target_factory` and declared per-target in `environment.yaml`.

**Layer 2: `tests/`** -- a pytest suite that uses labgrid targets and the custom drivers to run echo RTT benchmarks, throughput measurements, state machine validation, and issue-specific regression tests.

**Legacy: `lab/`** -- the older scenario-based runner (`python -m lab`) still works. It uses YAML scenarios and an inventory file instead of labgrid. Makefile targets like `make test-lab-2node` point at this runner.

## Project Structure

```
fips-lab/
  conftest.py              # pytest fixtures, benchmark collection, peer availability
  environment.yaml         # labgrid environment config (Phase 1: inline SSH)
  pyproject.toml           # package metadata and dependencies

  fips_lab/
    __init__.py            # imports all drivers + strategy for labgrid registration
    drivers/
      fips_service.py      # FipsServiceDriver (systemd/launchd start/stop/restart)
      fipsctl.py           # FipsctlDriver (show_peers, benchmark_echo, etc.)
      local_shell.py       # LocalShellDriver (subprocess on Mac, no SSH needed)
      esp_flash.py         # EspFlashDriver (esptool.py flashing via remote shell)
    strategy/
      fips.py              # FipsStrategy state machine (off -> deployed -> connected -> ready)

  tests/
    test_benchmark.py      # parametrized echo + throughput benchmarks
    test_strategy.py       # FipsStrategy lifecycle tests
    test_echo.py           # single echo test against ESP32
    test_issues.py         # regression tests for known issues (#132, #133)

  config/
    environment-coordinator.yaml  # Phase 2 labgrid config (place-based resources)
    exporter-218.yaml             # labgrid-exporter config for 218
    systemd/
      fips.service                # FIPS daemon unit for 218
      labgrid-exporter.service    # exporter unit for 218

  lab/                     # legacy scenario runner (still functional)
  scenarios/               # YAML scenarios for the legacy runner
  scripts/                 # publish-results.py, setup-218-phase2.sh
  inventory/               # lab.yaml (gitignored), lab.example.yaml
  results/                 # test output (gitignored)
```

## Lab Devices

Three targets defined in `environment.yaml`:

| Target | Role | Transport | BLE Adapter |
|--------|------|-----------|-------------|
| `macbook-local` | FIPS host, test controller | LocalShellDriver (subprocess) | macOS Bluetooth |
| `linux-218` | FIPS host | SSHDriver (via NetworkService to host "218") | hci0 |
| `esp32-d0wd-01` | microfips device | SSHDriver + EspFlashDriver (ESP32 attached to 218 via USB) | N/A |

Each target gets its own set of drivers and a FipsStrategy instance. The `fipsctl` binary path and FIPS config path are per-target in the environment config.

## Quick Start

```bash
make setup                    # install Python dependencies (via pip/requirements.txt)
```

Devices must be pre-built and reachable. FIPS binaries, `fipsctl`, and FIPS config files need to exist on each machine. fips-lab does not build or deploy FIPS itself.

### Starting FIPS on Mac

macOS CoreBluetooth doesn't grant Bluetooth permission to launchd-managed processes. FIPS on Mac must be started manually from Terminal (which has Bluetooth permission):

```bash
sudo RUST_LOG=debug FIPS_NOISE_KEYLOG=/tmp/fips-keylog-mac.txt \
  caffeinate -i /Users/macbook/src/fips/target/release/fips \
  --config /usr/local/etc/fips/fips.yaml > /tmp/fips.log 2>&1 &
```

`caffeinate -i` prevents macOS from sleeping and killing the process. The canonical config is at `config/fips/mac.yaml` (mirrored to `/usr/local/etc/fips/fips.yaml`).

FIPS on Linux (218) runs as a systemd service and starts automatically on boot.

Run the labgrid-based tests:

```bash
pytest --lg-env=environment.yaml tests/ -v
```

Run only benchmarks:

```bash
pytest --lg-env=environment.yaml tests/ -v -m benchmark
```

## Running Tests

### pytest + labgrid (primary)

The `--lg-env` flag tells labgrid which environment file to use for target resolution. Tests import `fips_lab` in `conftest.py`, which registers the custom drivers with labgrid.

Key markers:

- `@pytest.mark.benchmark` -- parametrized echo and throughput benchmarks
- `@pytest.mark.xfail` -- known failures (ESP32 L2CAP instability, issue #133)
- `with_mac_peer` / `with_esp32_peer` fixtures -- skip the test if the peer isn't connected

Test types:

- **Echo benchmarks** (`test_benchmark.py`) -- parametrized over payload sizes (0 to 256 bytes), measures RTT median and loss count across all device pairs
- **Throughput benchmarks** (`test_benchmark.py`) -- parametrized over frame sizes (20 to 100 bytes), measures achieved bitrate for upload direction
- **Strategy tests** (`test_strategy.py`) -- drives FipsStrategy through `off -> deployed -> connected -> ready`, verifies service state at each step
- **Issue tests** (`test_issues.py`) -- regression tests for documented issues (large payload loss on #133, download direction on #132)
- **Echo smoke** (`test_echo.py`) -- single echo test against ESP32 with xfail

Some test directions are skipped due to known limitations: Mac cannot initiate BLE scans (issue #128), ESP32 cannot initiate benchmarks, and download throughput is unimplemented (issue #132).

### Legacy scenario runner

The Makefile has targets that use the older `lab/` runner:

```bash
make test-lab-2node             # Mac initiates to Linux
make test-lab-2node-linux-init  # Linux initiates to Mac
make test-campaign-ble          # both directions back-to-back
make test-lab-3node             # Mac + Linux + ESP32
make test-microfips-smoke       # flash + smoke test
```

These require `inventory/lab.yaml` (gitignored, copy from `inventory/lab.example.yaml`).

## Test Results and Publishing

### Benchmark results (labgrid/pytest)

Benchmark tests feed results into a session-scoped `benchmark_results` fixture. At session end, results are written to `results/benchmark-matrix/` as a timestamped JSON file containing all measurements plus git info.

```bash
# Run benchmarks and auto-publish to Blossom + Nostr
pytest --lg-env=environment.yaml tests/test_benchmark.py --publish-benchmarks
# or publish the latest benchmark JSON
make publish-benchmarks
```

Results are published to Blossom + Nostr (kind 30078) with `PROJECT_TAG=fips-benchmark`. View them on the dashboard at [tests.tollgate.me](https://tests.tollgate.me/) — filter by `fips-benchmark` tag.

### Scenario results (legacy runner)

The legacy runner creates rich timestamped directories under `results/` with metrics timeseries, btmon captures, keylogs, iperf3 data, analysis, and SVG charts.

```bash
make test-lab-2node-publish     # run and publish
# or publish existing results
python3 scripts/publish-results.py results/<run-dir> --project-tag fips-ble
```

Scenario results are published with `PROJECT_TAG=fips-ble` and appear on [tests.tollgate.me](https://tests.tollgate.me/) under the `fips-ble` tag.

### Publishing setup

Both benchmark and scenario publishing use `scripts/publish-results.py`, which wraps `lib.result_publisher` (shared with physical-router-test-automation). Requires:
- `.nsec` file in repo root (same nsec as TollGate/prta testing)
- `nak` CLI installed (`curl -sL <nak-release-url> -o /usr/local/bin/nak && chmod +x $_`)

## Custom Drivers

### FipsServiceDriver (`fips_lab/drivers/fips_service.py`)

Manages the FIPS daemon through the OS service manager. Supports `systemd` (Linux) and `launchd` (macOS). On Linux, the `restart()` method also resets the BLE adapter (`hciconfig hci0 down/up`) to work around a kernel bug where the HCI LE Create Connection opcode returns `-EBUSY` after prolonged scanning.

### FipsctlDriver (`fips_lab/drivers/fipsctl.py`)

Wraps the `fipsctl` CLI. Methods: `show_status()`, `show_peers()`, `has_peer(npub)`, `benchmark_echo(peer, count, payload_size)`, `benchmark_throughput(peer, direction, duration, frame_size, rate)`. All methods return parsed JSON.

### LocalShellDriver (`fips_lab/drivers/local_shell.py`)

Implements labgrid's `CommandProtocol` using `subprocess.run`. Used for the Mac target where SSH isn't needed.

### EspFlashDriver (`fips_lab/drivers/esp_flash.py`)

Flashes firmware to ESP32 via `esptool.py` over a serial port. Typically invoked through an SSH-bound shell since the ESP32 is attached to the Linux host.

### FipsStrategy (`fips_lab/strategy/fips.py`)

State machine with states: `unknown -> off -> deployed -> connected -> ready`. The `transition()` method drives side effects at each step (stop service, start service, wait for peers). The `force()` method sets state without side effects.

## Isolation Policy

Lab FIPS nodes must not join the public FIPS network during testing. The legacy scenarios use an isolation policy:

```yaml
isolation:
  mode: lab-allowlist
  deny_unknown_peers: true
  write_peers_allow: true
  write_peers_deny_all: true
```

This writes `peers.allow` with lab device npubs and `peers.deny` containing `ALL`. FIPS ACL behavior is allow-first, then deny, so lab peers are permitted and everyone else is blocked.

## Phase 2: Coordinator/Exporter Architecture

The current setup (Phase 1) uses inline SSH resources in `environment.yaml`. Phase 2 moves to a labgrid coordinator/exporter model where 218 exports its resources (BLE adapter, serial port, network) and a coordinator matches them to places dynamically.

The configs are ready in `config/`:

- `config/exporter-218.yaml` -- defines what 218 exports (BluetoothAdapter hci0, SerialPort ttyUSB0, NetworkService)
- `config/environment-coordinator.yaml` -- like the current `environment.yaml` but uses place-based resource acquisition instead of inline SSH
- `config/systemd/fips.service` -- systemd unit for the FIPS daemon on 218, with keylog enabled and auto-restart
- `config/systemd/labgrid-exporter.service` -- systemd unit for the exporter on 218

Deploy Phase 2 to 218:

```bash
make setup-218-phase2          # push configs + systemd units to 218
make setup-218-phase2-dry-run  # preview what would be deployed
```

Currently blocked on 218 being online.

Deploy to 218:

```bash
make setup-218-phase2          # deploy canonical FIPS config + systemd units
make setup-218-phase2-dry-run  # preview what would be deployed
```

## Configuration Files

- `environment.yaml` -- labgrid target definitions, tracked in git. Contains driver bindings and binary paths for all three devices.
- `config/fips/mac.yaml` -- canonical FIPS config for Mac (BLE advertiser, no scan).
- `config/fips/linux-218.yaml` -- canonical FIPS config for 218 (BLE scanner + advertiser). Deployed to `/etc/fips/fips.yaml` by `setup-218-phase2.sh`.
- `config/systemd/fips.service` -- systemd unit for 218. Runs as root (fixes key permissions + control socket).
- `config/launchd/com.fips.daemon.plist` -- launchd plist for Mac (not usable yet -- macOS CoreBluetooth blocks Bluetooth from launchd).
- `config/environment-coordinator.yaml` -- Phase 2 variant using place-based resources instead of inline SSH.
- `inventory/lab.yaml` -- legacy runner device details, gitignored. Contains SSH hosts, binary paths, identities. Copy from `inventory/lab.example.yaml`.

## Makefile Targets

```
make setup                      install Python dependencies
make list                       list legacy scenarios
make dry-run-smoke              dry-run microfips smoke scenario
make dry-run-lab                dry-run 3-node isolated scenario
make dry-run-2node              dry-run 2-node BLE scenario
make dry-run-campaign-ble       dry-run bidirectional BLE campaign

make test-microfips-smoke       smoke test ESP32 + Linux
make test-lab-3node             3-node isolated mesh
make test-lab-2node             Mac initiates to Linux
make test-lab-2node-linux-init  Linux initiates to Mac
make test-lab-2node-publish     run 2-node and publish
make test-lab-2node-commit      run 2-node for a specific commit
make test-campaign-ble          bidirectional campaign
make test-campaign-ble-20min    extended campaign with publish

make publish                    publish latest scenario results to Blossom + Nostr
make publish-benchmarks         publish benchmark-matrix to Blossom + Nostr
make setup-218-phase2           deploy Phase 2 configs to 218
make setup-218-phase2-dry-run   preview Phase 2 deployment
make clean-results              remove results older than 30 days
```
