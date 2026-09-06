# AGENTS.md — fips-lab

> Physical-device BLE test framework for FIPS and microfips, built on
> [labgrid](https://labgrid.readthedocs.io/). Runs automated benchmarks and
> regression tests against real hardware: a MacBook, a Linux host, and ESP32
> boards. **Depends on [tollgate-lab](../tollgate-lab)** for shared
> infrastructure (SSH, ESP32 flash, hardware lock, reporting).

## What this repo does

fips-lab automates testing of the FIPS mesh-networking daemon over Bluetooth Low
Energy (BLE). It does **not** build or deploy FIPS itself — it assumes binaries,
`fipsctl`, and FIPS config already exist on each target machine. It then drives
those targets through labgrid to run echo/throughput benchmarks, strategy
lifecycle tests, and issue-specific regression tests, and publishes results to
Blossom + Nostr (visible at [tests.tollgate.me](https://tests.tollgate.me/)).

Two execution paths coexist:

1. **pytest + labgrid (primary)** — `tests/` uses labgrid targets declared in
   `environment.yaml`. Custom drivers in `fips_lab/` are registered with
   labgrid's `target_factory` via `import fips_lab` in `conftest.py`.
2. **Legacy scenario runner** — `python -m lab` reads YAML scenarios from
   `scenarios/` and an inventory file. Makefile targets like `test-lab-2node`
   use this. Still functional but being superseded by the labgrid path.

## Architecture (two layers on labgrid)

```
Layer 2: tests/           pytest suite — benchmarks, strategy, issue regressions
    ↑ uses
Layer 1: fips_lab/        custom labgrid drivers + FipsStrategy state machine
    ↑ depends on
tollgate-lab              shared infra (SSH, ESP flash, hardware lock, reporting)
    ↑ built on
labgrid                   resource → driver → protocol → strategy abstraction
```

### Custom drivers (`fips_lab/drivers/`)

| Driver | File | Protocol | Purpose |
|--------|------|----------|---------|
| `FipsServiceDriver` | `fips_service.py` | CommandProtocol | Start/stop/restart FIPS daemon via systemd (Linux) or launchd (macOS). Linux `restart()` also cycles `hciconfig hci0 down/up` to work around a kernel `-EBUSY` bug after prolonged scanning. |
| `FipsctlDriver` | `fipsctl.py` | CommandProtocol | Wraps the `fipsctl` CLI: `show_status()`, `show_peers()`, `has_peer()`, `benchmark_echo()`, `benchmark_throughput()`. All return parsed JSON. |
| `LocalShellDriver` | `local_shell.py` | CommandProtocol | `subprocess.run` on the Mac target (no SSH needed). |
| `EspFlashDriver` | `esp_flash.py` | — | Flashes ESP32 firmware via `esptool.py` over serial; typically invoked through an SSH-bound shell (ESP32 is USB-attached to the Linux host). |

### Strategy (`fips_lab/strategy/fips.py`)

`FipsStrategy` — state machine: `unknown → off → deployed → connected → ready`.
`transition()` drives side effects at each step (stop service, start service,
wait for peers). `force()` sets state without side effects.

## Lab devices (`environment.yaml`)

| Target | Role | Transport | BLE Adapter |
|--------|------|-----------|-------------|
| `macbook-local` | FIPS host, test controller | `LocalShellDriver` (subprocess) | macOS Bluetooth |
| `ai-legion-small` (formerly `linux-218`) | FIPS host | `SSHDriver` via `NetworkService` | `hci0` |
| `esp32-d0wd-01` | microfips device | `SSHDriver` + `EspFlashDriver` (ESP32 USB-attached to Linux host) | N/A |

Each target gets its own drivers and a `FipsStrategy` instance. `fipsctl` binary
path and FIPS config path are per-target in the environment config.

### Phase 2: coordinator/exporter mode

Phase 1 (current) uses inline SSH resources in `environment.yaml`. Phase 2
(`config/environment-coordinator.yaml`, `config/exporter-218.yaml`) moves to a
labgrid coordinator/exporter model where the Linux host exports its resources
(BLE adapter, serial port, network) and a coordinator matches them to places
dynamically. Deployed via `make setup-218-phase2`. Systemd units live in
`config/systemd/`.

## Testing protocol

### Primary: pytest + labgrid

```bash
# All tests
pytest --lg-env=environment.yaml tests/ -v

# Benchmarks only
pytest --lg-env=environment.yaml tests/ -v -m benchmark

# Specific suite
pytest --lg-env=environment.yaml tests/test_benchmark.py -v
```

The `--lg-env` flag tells labgrid which environment file to use. `import fips_lab`
in `conftest.py` registers the custom drivers. The `--wait-peers-timeout=N`
option (default 120s) controls how long to wait for BLE peers to connect before
failing.

**Markers:**
- `@pytest.mark.benchmark` — parametrized echo + throughput benchmarks
- `@pytest.mark.xfail` — known failures (ESP32 L2CAP instability, issue #133)
- `with_mac_peer` / `with_esp32_peer` fixtures — skip if peer isn't connected

**Test files (`tests/`):**
- `test_benchmark.py` — echo RTT (payload 0–256 B) and throughput (frame 20–100 B) across all device pairs
- `test_strategy.py` — drives `FipsStrategy` through `off → deployed → connected → ready`
- `test_issues.py` — regressions for documented issues (#132 download, #133 large payload loss)
- `test_echo.py` — single echo smoke test against ESP32 (xfail)
- `test_esp32_l2cap.py` — ESP32 BLE L2CAP structured regressions
- `test_flash_and_verify.py` — ESP32 flash + boot verification

**Skipped directions (known limitations):** Mac cannot initiate BLE scans
(issue #128), ESP32 cannot initiate benchmarks, download throughput is
unimplemented (issue #132).

### Legacy: scenario runner (`python -m lab`)

```bash
make test-lab-2node             # Mac initiates to Linux
make test-lab-2node-linux-init  # Linux initiates to Mac
make test-campaign-ble          # bidirectional, back-to-back
make test-lab-3node             # Mac + Linux + ESP32
make test-microfips-smoke       # flash + smoke test
```

Requires `inventory/lab.yaml` (gitignored — copy from
`inventory/lab.example.yaml`). Produces rich timestamped result directories
under `results/` with metrics timeseries, btmon captures, keylogs, iperf3 data,
analysis, and SVG charts.

### Results & publishing

Benchmark tests feed a session-scoped `benchmark_results` fixture → written to
`results/benchmark-matrix/` as timestamped JSON (measurements + git info).

```bash
pytest --lg-env=environment.yaml tests/test_benchmark.py --publish-benchmarks
make publish-benchmarks         # publish latest benchmark JSON
make publish                    # publish latest scenario results (PROJECT_TAG=fips-ble)
```

Publishing uses `scripts/publish-results.py` → `lib.result_publisher` (shared
with physical-router-test-automation via nostr-publish). Requires:
- `.nsec` file in repo root (same nsec as TollGate/prta testing)
- `nak` CLI installed

Benchmark results → tag `fips-benchmark`; scenario results → tag `fips-ble`.
Both appear on [tests.tollgate.me](https://tests.tollgate.me/).

## BLE capture decryption tool: `lab/capture/btsnoop_decrypt.py`

Post-test decryption pipeline for btsnoop HCI captures (from `btmon`). Parses
the capture, reassembles HCI ACL fragments into L2CAP frames, extracts FIPS
L2CAP traffic (PSM 133), parses FMP frames, and decrypts Noise-encrypted
payloads using keylog files. Output is **privacy-filtered** — only aggregate
counts and message-type breakdowns, never raw payloads, keys, or BLE addresses.

### Running capture analysis

```bash
# Library/CLI usage — run_dir must contain btmon.btsnoop + keylog-*.txt
python -m lab.capture.btsnoop_decrypt results/<run-dir>

# Frame-level diagnostics for every frame that fails decryption
python -m lab.capture.btsnoop_decrypt results/<run-dir> --debug-failures
```

Or as a library:
```python
from lab.capture.btsnoop_decrypt import decrypt_btsnoop_capture
summary = decrypt_btsnoop_capture(Path("results/<run-dir>"), debug_failures=True)
```

### Pipeline stages

1. **`parse_btsnoop()`** — btsnoop v1 parser. Supports both HCI_UART (1002) and
   monitor-mode (2001) datalink types. Monitor mode strips the HCI type byte;
   `_monitor_type_byte()` reconstructs it from flags.
2. **`reassemble_acl_packets()`** — reassembles HCI ACL PB=0/PB=2 (first) and
   PB=1 (continuation) fragments into complete L2CAP frames, tracked per
   connection handle.
3. **`extract_fips_l2cap_frames()`** — tracks L2CAP CoC connections via
   signalling (CID 0x0001) to find PSM 133. Falls back to content-based FMP
   detection on dynamic CIDs (≥0x0040) when btmon started after connection setup.
4. **`parse_fmp_frames()`** — strips BLE transport 2-byte length prefix, parses
   FMP common prefix (version, phase, flags, payload_len).
5. **`_decrypt_established_frame()`** — ChaCha20-Poly1305 AEAD decryption.
   AAD = 16-byte FMP header; nonce = `[0x00×4][counter u64 LE]`. Tries all
   send/recv keys from keylog entries.

### Output artifacts (written into `run_dir`)

- `decryption-summary.json` — full aggregate statistics
- `decryption-summary.md` — human-readable report (FMP frame types, decryption
  stats, message-type breakdown, per-rekey groups, rekey interval distribution,
  handshake analysis, direction breakdown)
- `decrypted-fmp.pcapng` — decrypted frames as pcapng (LINKTYPE_USER0) for
  Wireshark/tshark inspection with the FMP Lua dissector. Each packet = 16-byte
  outer FMP header (AAD) + decrypted plaintext.

### The decryption failure rate (~0.19%)

The summary reports a `failure_pct` — the percentage of established-phase FMP
frames that failed decryption against **all** keys in the keylog. In healthy
captures this sits around **0.19%**. This residual failure rate is **expected**
and is caused by **HCI ACL fragment reassembly corruption** (see Known Issues),
not by missing keys or protocol errors. A sudden jump above ~1% indicates a
real problem (keylog gaps, capture corruption, or an FMP parsing regression).

### `--debug-failures` flag (new — frame-level diagnostics)

Added in commit `c2d5a45` (closes #2). When passed, every frame that fails
decryption prints a one-line diagnostic:

```
[FAIL] frame #1234 ts=1234.567890 handle=0x000a dir=TX len=80 payload=aabbcc...
```

Fields: frame index, timestamp (seconds), HCI connection handle, direction
(TX/RX), total frame size, first 16 bytes of payload (hex, truncated to 32
chars). A grouped summary (by connection handle, then by direction) is **always
printed** when any failures occur, even without the flag. Up to 50 failed-frame
details are recorded in `decryption-summary.json` under `failed_frames`.

### Related capture modules (`lab/capture/`)

| Module | Purpose |
|--------|---------|
| `btmon.py` | btmon capture control (start/stop) |
| `btsnoop_decrypt.py` | decryption pipeline (above) |
| `keylog.py` | Noise keylog parsing — `FIPS_LINK`/`FIPS_SESSION` lines with 4× 64-hex-char fields (local npub, peer npub, send key, recv key) |
| `correlate.py` | cross-correlate captures across devices |
| `iperf.py` | iperf3 throughput capture |
| `rssi.py` | RSSI measurement |
| `serial_log.py` | ESP32 serial console logging |
| `tshark.py` | tshark-based analysis |

## Known issues

### HCI ACL fragment reassembly corruption

**Symptom:** ~0.19% of established FMP frames fail decryption against all keys.
**Root cause:** the Linux HCI controller occasionally delivers ACL continuation
fragments (PB=1) out of order or drops one, so `reassemble_acl_packets()` emits
a corrupted L2CAP SDU whose ciphertext no longer matches any AEAD nonce/counter.
This is a controller/kernel-level artifact, not a bug in FIPS or this pipeline.
**Mitigation:** the `--debug-failures` flag now surfaces per-frame details
(handle, direction, timestamp, payload preview) so failures can be localized to
specific connection handles and correlated with btmon timestamps. The failure is
cosmetic for aggregate statistics (the ~0.19% is well within noise) but matters
for per-frame pcapng reconstruction.

### Issue #128 — Mac cannot initiate BLE scans

macOS CoreBluetooth doesn't grant Bluetooth permission to launchd-managed
processes, so FIPS on Mac cannot scan. FIPS on Mac must be started manually from
Terminal (which has Bluetooth permission). Canonical Mac config:
`config/fips/mac.yaml` (BLE advertiser, no scan).

### Issue #132 — download throughput unimplemented

Download-direction throughput benchmark is not yet implemented; those test
directions are skipped.

### Issue #133 — large payload loss (ESP32 L2CAP instability)

ESP32 L2CAP has known instability with large payloads; covered by `@xfail`.

### Kernel `-EBUSY` after prolonged scanning (Linux)

The HCI `LE Create Connection` opcode returns `-EBUSY` after prolonged BLE
scanning. `FipsServiceDriver.restart()` on Linux cycles the BLE adapter
(`hciconfig hci0 down/up`) to work around this.

## Configuration files

| File | Purpose |
|------|---------|
| `environment.yaml` | labgrid target definitions (Phase 1, inline SSH) — tracked in git |
| `environment-coordinator.yaml` | Phase 2 labgrid config (place-based resource acquisition) |
| `config/fips/mac.yaml` | canonical FIPS config for Mac (BLE advertiser, no scan) |
| `config/fips/linux-218.yaml` | canonical FIPS config for Linux host (scanner + advertiser) |
| `config/exporter-218.yaml` | labgrid exporter config for the Linux host (hci0, ttyUSB0, NetworkService) |
| `config/systemd/fips.service` | FIPS daemon systemd unit (runs as root) |
| `config/systemd/labgrid-exporter.service` | exporter systemd unit |
| `config/launchd/com.fips.daemon.plist` | launchd plist for Mac (not usable — macOS CoreBluetooth blocks launchd) |
| `inventory/lab.yaml` | legacy runner device details — gitignored; copy from `inventory/lab.example.yaml` |
| `pytest.ini` / `conftest.py` | pytest config, markers, fixtures, benchmark collection |

## Isolation policy

Lab FIPS nodes must **not** join the public FIPS network during testing. Legacy
scenarios use:

```yaml
isolation:
  mode: lab-allowlist
  deny_unknown_peers: true
  write_peers_allow: true
  write_peers_deny_all: true
```

Writes `peers.allow` with lab device npubs and `peers.deny` containing `ALL`.
FIPS ACL is allow-first then deny, so lab peers are permitted and everyone else
blocked.

## Key commands

```bash
make setup                      # install Python deps
make test-labgrid               # pytest + labgrid, all tests
make test-labgrid-benchmark     # benchmarks only
make test-labgrid-strategy      # strategy lifecycle tests
make test-labgrid-issues        # issue regressions
make test-lab-2node             # legacy: Mac → Linux
make test-campaign-ble-20min    # legacy: 20-min bidirectional campaign + publish
make publish-benchmarks         # publish latest benchmark JSON to Blossom + Nostr
make setup-218-phase2           # deploy Phase 2 configs to Linux host
make clean-results              # remove results older than 30 days

# BLE capture analysis
python -m lab.capture.btsnoop_decrypt results/<run-dir> --debug-failures
```

## Dependencies

- **tollgate-lab** (`>=0.1.0`) — shared SSH, ESP flash, hardware lock, reporting
- **labgrid** (`>=25.0`) — device orchestration framework
- **pytest** (`>=8.0`), **paramiko**, **pyserial**, **attrs**, **pyyaml**
- `cryptography` (lazy-imported in `btsnoop_decrypt.py` for ChaCha20-Poly1305)
- `nak` CLI + `.nsec` for Blossom/Nostr publishing

## Multi-session coordination (2026-09-06)

More than one agent session shares this bench host. `acquire_board_lock()`
announces a session-registry entry automatically; before ANY bench/device
work run `python3 -m tollgate_lab.session_registry status` (live sessions,
claimed resources, registered PIDs; `LOST` = a session died mid-work —
investigate before inheriting). Long runs register children via
`session.add_pid()`; process hygiene uses `lab-kill <session-name>`, never
`pkill -f` patterns. Full stack + checklist + journal contract:
hackathon-tooling `patterns/testing/multi-session-coordination.md`.

## External posting (owner directive 2026-09-06 — CHANNEL rule)

Agents never post on non-member repos — no `gh` writes (issues, PRs,
comments, reviews, gists), not even with per-text owner sign-off; the
owner does the copy-paste into GitHub themselves. Member orgs (verify:
`gh api user/orgs`; 2026-09-06: Amperstrand, OpenTollGate, net4sats,
FreedomTechFeed) keep the existing owner-gate flow. Read the target
repo CONTRIBUTING/AI policy before drafting anything upstream.
Canonical text: lightning-playground AGENTS.md (standing rule UPDATE
2026-09-06).
