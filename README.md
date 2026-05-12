# fips-lab

Physical-device orchestration for testing `Amperstrand/fips` and `Amperstrand/microfips` on a private lab testbed.

This repo is intentionally separate from upstream FIPS. Upstream keeps Docker/CI chaos tests; this lab adds real hardware coverage: BLE hosts, ESP32/microfips boards, routers, old laptops, and later Windows/OpenWrt machines.

## How It Works

fips-lab is an **orchestration layer** — it does not compile code or flash firmware itself. It coordinates pre-built binaries on real hardware, runs test scenarios, and collects results.

### The Build-Deploy-Test Pipeline

```
┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐
│  Developer   │    │  fips-lab   │    │  Physical Devices    │
│  (you)       │    │  (MacBook)  │    │                      │
│              │    │             │    │  ┌───────────────┐   │
│ Builds fips  │───▶│ Orchestrates│───▶│ │ MacBook       │   │
│ on target    │    │ via scenario│    │ │ (local)       │   │
│ machines     │    │ + inventory │    │ ├───────────────┤   │
│              │    │             │    │ │ Linux host    │   │
│              │    │ Collects    │◀───│ │ (SSH)         │   │
│              │    │ metrics,    │    │ ├───────────────┤   │
│              │    │ captures,   │    │ │ ESP32 boards  │   │
│              │    │ analysis    │    │ │ (serial)      │   │
│              │    │             │    │ └───────────────┘   │
└─────────────┘    └─────────────┘    └─────────────────────┘
```

### Who Does What

| Step | Who | How |
|------|-----|-----|
| **Build FIPS (macOS)** | Developer, on the MacBook | `cd ~/src/fips && cargo build --release --features ble-macos` |
| **Build FIPS (Linux)** | Developer, via SSH to Linux host | `ssh 218 "cd ~/fips && cargo build --release"` |
| **Build microfips firmware** | Developer, on any machine | `cd ~/src/microfips && cargo build --release` (produces `.bin`) |
| **Deploy FIPS to Mac** | fips-lab (DeployManager) | Restarts via `caffeinate -i fips --config ...` (local process) |
| **Deploy FIPS to Linux** | fips-lab (DeployManager) | Restarts via `ssh 218 "nohup fips --config ..."` |
| **Flash microfips to ESP32** | Developer (manual, for now) | `esptool.py --chip esp32 ... write_flash 0x0 firmware.bin` |
| **Query metrics** | fips-lab (Device layer) | `fipsctl --socket /path show peers` (local or via SSH) |
| **Capture BLE traffic** | fips-lab (BtmonCapture) | `ssh 218 "sudo btmon -i hci0 -w capture.btsnoop"` then `scp` back |
| **Collect keylogs** | fips-lab (KeylogCapture) | Reads `FIPS_NOISE_KEYLOG` files from Mac (local) and Linux (SSH) |
| **Measure throughput** | fips-lab (IperfSession) | Runs `iperf3` between Mac and Linux over the FIPS TUN interface |
| **Collect RSSI** | fips-lab (RssiCollector) | `ssh 218 "sudo hcitool rssi <addr>"` on the Linux BLE adapter |
| **Analyze results** | fips-lab (analysis.py) | Reads metrics timeseries, produces verdict + charts |
| **Publish reports** | fips-lab (publish-report.sh) | Pushes to `gh-pages` branch of fips-lab repo |

### Before Running a Test

fips-lab expects the binaries to already be built and in place. The inventory (`inventory/lab.yaml`) tells fips-lab where to find them:

```yaml
devices:
  macbook-local:
    fips_binary: /Users/macbook/src/fips/target/release/fips    # must exist
    fipsctl: /Users/macbook/src/fips/target/release/fipsctl     # must exist
    config_path: /usr/local/etc/fips/fips.yaml                  # must exist

  linux-218:
    transport: ssh
    host: "218"
    fips_binary: /home/ubuntu/fips/target/release/fips          # must exist on host
    fipsctl: /home/ubuntu/fips/target/release/fipsctl           # must exist on host
    config_path: /etc/fips/fips.yaml                            # must exist on host
```

**To test a new commit**, you must:
1. Check out the branch/commit in the fips repo on both machines
2. Build on both machines
3. Then run `make test-lab-2node` or `make test-campaign-ble`

fips-lab does **not** `git pull` or `cargo build` — that's your responsibility. It records the current commit hash in `metadata.json` for provenance.

## Design

fips-lab mirrors the upstream chaos runner shape:

1. Load a YAML scenario (or a campaign of multiple scenarios).
2. Resolve real devices from an inventory.
3. Generate isolated lab ACL artifacts (`peers.allow`, `peers.deny`).
4. Stop any running FIPS instances, restart them with keylog enabled.
5. Poll until nodes are ready (respond to `fipsctl show status`).
6. Run a metrics collection loop for the scenario duration.
7. Stop captures, collect keylogs, run iperf3 throughput tests.
8. Analyze results and produce a verdict (PASS / FAIL / DEGRADED).

The first priority is not broad CI. It is **repeatable physical evidence for a specific git commit and device set**.

## Device Types

Real device details live in `inventory/lab.yaml`, which is gitignored.

| Transport | Device class | Example | How fips-lab communicates |
|-----------|-------------|---------|--------------------------|
| `local` | FIPS host (macOS) | This MacBook | Runs `fipsctl` and process commands directly |
| `ssh` | FIPS host (Linux) | Linux box "218" | `ssh user@host` for all commands |
| `serial` | microfips (local ESP32) | USB-attached ESP32 | Serial port for log streaming |
| `serial-via-ssh` | microfips (remote ESP32) | ESP32 attached to Linux host | SSH tunnel to serial port |

## Scenarios

Scenarios are YAML files in `scenarios/` that define what to test:

- **`lab-2node-ble.yaml`** — Mac initiates → Linux. 10 min BLE mesh with btmon, keylog, iperf3.
- **`lab-2node-ble-linux-init.yaml`** — Linux initiates → Mac. Same tests, opposite direction.
- **`lab-3node-isolated.yaml`** — Mac + Linux + ESP32. Isolated 3-node mesh.
- **`microfips-smoke.yaml`** — Flash one ESP32, start one Linux host, 5 min BLE connection test.

### Campaigns

A **campaign** runs multiple scenarios back-to-back and produces a combined report. This is useful for testing both BLE initiator directions in one run:

```yaml
# scenarios/campaign-ble-bidirectional.yaml
campaign:
  name: ble-bidirectional
  description: Run both BLE initiator directions (mac→linux and linux→mac) back-to-back
  scenarios:
    - scenarios/lab-2node-ble.yaml
    - scenarios/lab-2node-ble-linux-init.yaml
```

Campaign results include a `campaign-summary.md` with side-by-side comparison of metrics across both directions.

## Quick Start

```bash
make setup                    # install Python dependencies
cp inventory/lab.example.yaml inventory/lab.yaml
$EDITOR inventory/lab.yaml    # adjust paths, hosts, identities
make list                     # show available scenarios
make dry-run-smoke            # verify everything without touching devices
```

A dry run does not touch devices. It verifies scenario loading, inventory resolution, lab ACL generation, metadata, snapshots, and result directory layout.

## Running Tests

### Single scenario

```bash
make test-lab-2node           # Mac initiates → Linux
make test-lab-2node-linux-init # Linux initiates → Mac
```

### Campaign (both directions)

```bash
make test-campaign-ble        # Runs both scenarios, produces combined report
```

### What happens during a test run

1. **ACL setup** — writes `peers.allow` with lab device npubs and `peers.deny` containing `ALL`
2. **Capture setup** — starts `btmon` on Linux (SSH) for BLE HCI capture
3. **Deploy** — kills existing FIPS processes, restarts with `FIPS_NOISE_KEYLOG` env var
4. **Readiness poll** — waits up to 60s for each node to respond to `fipsctl show status`
5. **Warmup** — 30s for BLE discovery and initial connection
6. **Metrics loop** — collects `show_status`, `show_peers`, `show_mmp` at 30s intervals
7. **RSSI collection** — polls `hcitool rssi` on Linux for signal strength
8. **Capture stop** — stops btmon, copies btsnoop file via `scp`
9. **Keylog collection** — reads keylog files from Mac (sudo) and Linux (SSH + sudo)
10. **iperf3** — TCP and UDP throughput tests over the FIPS TUN interface
11. **BTSnoop decryption** — decrypts BLE capture using keylog keys
12. **Analysis** — produces verdict, assertions, charts (RTT, peer count, rekeys, RSSI)

### Results

Each run creates a timestamped directory under `results/`:

```
results/20260508-143000-lab-2node-ble/
├── metadata.json              # timestamp, git commits, device info
├── scenario.yaml              # copy of the scenario file
├── devices.yaml               # resolved inventory devices
├── snapshot-initial.json      # metrics at start
├── snapshot-final.json        # metrics at end
├── metrics-timeseries.json    # all metric samples over time
├── capture-results.json       # btmon, serial capture info
├── keylog-results.json        # keylog collection stats
├── iperf3-results.json        # throughput test results
├── analysis.json              # structured verdict + metrics
├── analysis.md                # human-readable report
├── chart-rtt.svg              # RTT over time
├── chart-peers.svg            # peer count over time
├── chart-rekeys.svg           # rekey events + disconnects
├── chart-rssi.svg             # BLE signal strength
├── btmon.btsnoop              # raw BLE capture
├── keylog-mac.txt             # Noise keys from Mac
└── keylog-linux.txt           # Noise keys from Linux
```

### Publishing Reports

```bash
make test-lab-2node-publish    # run test and publish to gh-pages
# or after a run:
bash scripts/publish-report.sh results/20260508-143000-lab-2node-ble
```

Publishing copies results to a `gh-pages` branch, generates an HTML dashboard with per-commit test history, verdict trends, and a device compatibility matrix. Keylogs, captures, and device paths are redacted before publishing.

## Isolation Policy

Lab FIPS nodes should not join the public/broader FIPS network while testing.

Scenarios use:

```yaml
isolation:
  mode: lab-allowlist
  deny_unknown_peers: true
  write_peers_allow: true
  write_peers_deny_all: true
```

The runner writes `peers.allow` with lab device npubs and `peers.deny` containing `ALL`. FIPS ACL behavior is allow-first, then deny — lab peers are permitted, everyone else is blocked.

## Assertions

Each scenario is evaluated against a set of assertions:

| Assertion | Pass condition |
|-----------|---------------|
| All expected peers connected | Every link in `topology.links` shows `connected` |
| MMP loss < 5% | Max loss rate across all samples is under 5% |
| Keylog coverage | Keylog files contain keys for all connected pairs |
| No loop errors | No `"error"` keys in metrics timeseries |
| No disconnects | No peer disappearances between consecutive samples |

The overall verdict is determined by:

- **PASS** — all assertions pass
- **DEGRADED** — connectivity passes but metric assertions fail
- **FAIL** — connectivity assertions fail
- **INSUFFICIENT_DATA** — no timeseries data collected

## Configuration Files

### FIPS node configs

Each device in the inventory points to a FIPS config file on that machine:

- Mac: `/usr/local/etc/fips/fips.yaml`
- Linux: `/etc/fips/fips.yaml`

These must be pre-configured with the correct identity, BLE transport settings, and ACL paths. fips-lab does not write these — it only restarts FIPS with them.

### Inventory

`inventory/lab.yaml` (gitignored) contains real device details: SSH hosts, binary paths, control sockets, BLE adapter names, identities, and serial ports. Copy from `inventory/lab.example.yaml` and adjust.
