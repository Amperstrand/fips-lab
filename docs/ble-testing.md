# BLE Transport Testing

Hardware-in-the-loop BLE transport testing for FIPS. These tests require
physical Bluetooth Low Energy adapters and cannot run in CI (no Bluetooth
hardware in GitHub Actions). All BLE transport logic is tested via
`MockBleIo` unit tests that run in CI without hardware — this directory is
for **validation against real BLE radios**.

## Prerequisites

### Linux

- BLE adapter (built-in or USB dongle, e.g., CSR8510, Intel AX200)
- BlueZ stack: `sudo apt install bluetooth bluez`
- Verify adapter: `hciconfig hci0` or `btmgmt info`
- The FIPS binary must be built with BlueZ support (automatic on glibc):
  ```
  cargo build --release
  ```

### macOS

- Built-in Bluetooth (all Macs since 2012)
- Build with the `ble-macos` feature:
  ```
  cargo build --release --features ble-macos
  ```

### Wireshark (optional, for protocol analysis)

- Install Wireshark with BLE capture support
- Copy `testing/chaos/wireshark/fips-dissector.lua` to your Wireshark
  plugins directory (`~/.local/lib/wireshark/plugins/` on Linux,
  `~/.config/wireshark/plugins/` on macOS)
- Capture BLE traffic: `sudo btmon -i hci0 -w capture.log` (Linux)

## Running Tests

### Unit tests (no hardware required)

BLE transport logic is tested via `MockBleIo` in-memory channel doubles
that run in CI without Bluetooth hardware:

```bash
cargo test --lib                  # All unit tests (includes 65 BLE tests)
cargo test --lib -- transport::ble  # BLE-specific tests only
cargo test --lib -- transport::ble::backoff  # Single module
```

### Two-box stability test (requires hardware)

The most common setup is a Mac and a Linux box within BLE range:

1. **Build on both machines** (see Prerequisites above)
2. **Start the Linux node**:
   ```bash
   fips -c linux-node.yaml
   ```
   Where `linux-node.yaml` contains:
   ```yaml
   transports:
     ble:
       adapter: "hci0"
       advertise: true
       scan: true
       auto_connect: true
       accept_connections: true
   ```
3. **Start the macOS node**:
   ```bash
   fips -c macos-node.yaml
   ```
   Where `macos-node.yaml` contains:
   ```yaml
   transports:
     ble:
       adapter: "default"
       advertise: true
       scan: true
       auto_connect: true
       accept_connections: true
   ```
4. **Wait for convergence** — typically 3–5 seconds. Check logs for:
   ```
   BLE transport started adapter=hci0 mtu=2048
   BLE peer discovered addr=AA:BB:CC:DD:EE:FF
   FMP handshake completed peer=<npub>
   Tree converged root=<npub> depth=0 peers=1
   ```

### Automated lab tests (fips-lab)

The `fips-lab` repo orchestrates physical-device tests with deploy/restart, metrics collection, capture, and analysis:

```bash
cd ~/src/fips-lab

# Single direction (Mac initiates)
make test-lab-2node

# Single direction (Linux initiates)
make test-lab-2node-linux-init

# Both directions back-to-back with combined report
make test-campaign-ble

# Dry run (no devices touched)
make dry-run-2node
```

fips-lab handles FIPS restart, BLE discovery warmup, metrics collection, btmon capture, keylog extraction, iperf3 throughput, and produces a full analysis report with verdict.

Results are saved to `fips-lab/results/<timestamp>-<scenario>/`.

See the fips-lab README for full documentation on the build-deploy-test pipeline.

### Legacy stability test script

The standalone `ble-stability-test.sh` script is also available for manual testing:

```bash
cd ~/src/fips-lab

# 20-minute default
./ble-stability-test.sh

# Custom duration + iperf3 throughput test
./ble-stability-test.sh -d 60 --iperf

# With BLE traffic capture
./ble-stability-test.sh --capture -v
```

This script is a lower-level alternative — it does not produce structured analysis or publish reports.

## Expected Results

For the `ble-smoke` scenario (2 nodes):

| Metric | Observed |
| ------ | -------- |
| BLE scan to discovery | ~200–500ms |
| GATT PSM read | ~286ms |
| L2CAP channel open | ~29ms |
| Noise handshake | ~50ms |
| **Total connect** | **~2300ms** |
| MTU | 2048/2048 (both directions) |
| Spanning tree convergence | < 5 seconds total |
| Throughput | 50–250 Kbps (limited by BLE link) |

### Hardware-validated setup

| Box | Role | Adapter |
| --- | ---- | -------- |
| macOS (arm64) | Central + Peripheral | Built-in (`default`) |
| Linux (x86_64) | Central + Peripheral | `hci0` |

BLE PSM: dynamic (GATT-advertised by macOS via UUID `9c90b790-2cc5-42c0-9f87-c9cc40648f4c`).
The default PSM in FIPS config is `0x0085` (133), but macOS CoreBluetooth
allocates the actual PSM dynamically and publishes it via GATT. The Linux
node reads the PSM from GATT during discovery.

## Troubleshooting

### "No BLE adapter found"

- Linux: Check `hciconfig hci0` — adapter may be blocked. Run
  `sudo rfkill unblock bluetooth` and `sudo hciconfig hci0 up`.
- macOS: Check System Preferences → Bluetooth is enabled.

### "L2CAP connect failed"

- Linux: Ensure `bluetoothd` is running (`sudo systemctl status bluetooth`).
- Check PSM 0x0085 (133) is not in use: `sudo sdptool browse local`.
- BLE devices must be paired or have BLE enabled (different from classic
  Bluetooth pairing).

### macOS central role receives no data

- This was a bluest bug: L2CAP `NSInputStream` was scheduled on
  `mainRunLoop()` which is never pumped in CLI/tokio apps.
- **Fixed** in the Amperstrand/bluest fork (Amperstrand/bluest#3).
- If using upstream bluest, the peripheral path works
  (uses its own dispatch queue) but the central path cannot receive.
- The fork is pinned in `Cargo.toml` via `rev = "f3c8d09"`.

### macOS L2CAP sends silently lose bytes

- This was a bluest bug: `OutputStreamDelegate::send_packet` discarded
  bytes on partial `NSOutputStream.write(maxLength:)`.
- **Fixed** in the Amperstrand/bluest fork (Amperstrand/bluest#2).
- Symptom: intermittent corruption in Noise handshake or AEAD decryption
  failures under sustained traffic.

### TCP over BLE is slow and bursty

- TCP works over BLE but is suboptimal due to the constrained link and
  kernel-level TCP behavior.
- **Observed**: iperf3 TCP -w 8K delivers ~8 KB/s in 15-second retransmission
  bursts (deterministic RTO pattern). Forward direction sends 128KB burst in
  the first second then stalls (macOS wscale caching). Reverse direction
  achieves ~24 KB/s (limited by 2920-byte window / 100ms RTT).
- **UDP and ICMPv6** (ping6, iperf3 UDP mode) work reliably at up to 80 Kbps
  with zero loss. Use UDP for BLE throughput testing.
- This is a fundamental BLE bandwidth constraint combined with kernel TCP
  behavior, not a FIPS bug.

### Connection keeps dropping

- BLE connection interval may be too aggressive. Default is 30ms; some
  adapters prefer 100ms+.
- Check distance — BLE range is typically 10m (line of sight).
- USB BLE dongles may have power issues — try a powered USB hub.

### "MockBleIo" in logs

This means the transport compiled with the mock backend instead of the real
one. Ensure:
- Linux: Building with glibc (not musl) so `bluer_available` is set
- macOS: `--features ble-macos` is specified

## ble_spike.rs

`ble_spike.rs` is a standalone hardware validation tool that exercises
the `bluer` crate's L2CAP CoC directly (outside of the FIPS transport
layer). It is useful for verifying that BLE hardware and BlueZ are
working correctly before running the full FIPS BLE stack.

```bash
cd testing/ble
cargo run --bin ble_spike
```

This is a development spike — the production BLE transport in `src/transport/ble/`
uses the `BleIo` trait abstraction with platform-specific backends.
