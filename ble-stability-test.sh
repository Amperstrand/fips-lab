#!/bin/bash
# BLE two-box stability test: macOS ↔ Linux over BLE.
#
# Runs FIPS on both machines, monitors connectivity for a configurable
# duration, and reports MMP loss, rekey success, goodput, and errors.
#
# Prerequisites:
#   - macOS host with FIPS built (--features ble-macos)
#   - Linux host reachable via SSH (default: localhost)
#   - Both machines within BLE range (~10m)
#   - Passwordless sudo on both hosts
#
# Usage:
#   ./testing/ble/ble-stability-test.sh [options]
#
# Options:
#   -d, --duration <mins>   Test duration in minutes (default: 20)
#   -r, --rekey <secs>      Rekey interval in seconds (default: 60)
#   --linux <host>          Linux SSH host/alias (default: localhost)
#   --linux-user <user>     Linux SSH user (default: $USER)
#   --linux-path <path>     Path to FIPS binary on Linux (required unless --linux is omitted)
#   --fips <path>           Path to macOS FIPS binary (default: auto-detect)
#   --no-ping               Skip ping6 traffic
#   --iperf                 Run iperf3 after stability test
#   --capture               Capture BLE traffic via btmon on Linux
#   --check-interval <secs> Interval between health checks (default: 60)
#   -v, --verbose           Verbose output
#   -h, --help              Show this help
#
# Examples:
#   ./testing/ble/ble-stability-test.sh                    # 20-minute test
#   ./testing/ble/ble-stability-test.sh -d 60              # 1-hour test
#   ./testing/ble/ble-stability-test.sh -d 5 --iperf       # 5-min test + iperf3
#   ./testing/ble/ble-stability-test.sh --capture -v       # With btmon capture
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DEFAULT_DURATION=20
DEFAULT_REKEY=60
DEFAULT_LINUX_HOST="localhost"
DEFAULT_LINUX_USER="${USER:-root}"
DEFAULT_LINUX_PATH=""
DEFAULT_CHECK_INTERVAL=60
DEFAULT_RATE_BPS=200000
DEFAULT_MTU=2048

DURATION=$DEFAULT_DURATION
REKEY=$DEFAULT_REKEY
LINUX_HOST="$DEFAULT_LINUX_HOST"
LINUX_USER="$DEFAULT_LINUX_USER"
LINUX_PATH="$DEFAULT_LINUX_PATH"
FIPS_PATH=""
NO_PING=""
DO_IPERF=""
CAPTURE=""
CHECK_INTERVAL=$DEFAULT_CHECK_INTERVAL
VERBOSE=""

SSH="ssh -o ConnectTimeout=10 -o BatchMode=yes"
SCP="scp -o ConnectTimeout=10 -o BatchMode=yes"

usage() {
    sed -n '3,/^$/p' "$0" | sed 's/^# //' | sed 's/^#//'
    exit 0
}

log()  { echo "[$(date -u +%H:%M:%SZ)] $*"; }
vlog() { [ -n "$VERBOSE" ] && echo "[$(date -u +%H:%M:%SZ)] [debug] $*" || true; }
warn() { echo "[$(date -u +%H:%M:%SZ)] [WARN] $*" >&2; }
fail() { echo "[$(date -u +%H:%M:%SZ)] [FAIL] $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -d|--duration)        DURATION="$2"; shift 2 ;;
        -r|--rekey)           REKEY="$2"; shift 2 ;;
        --linux)              LINUX_HOST="$2"; shift 2 ;;
        --linux-user)         LINUX_USER="$2"; shift 2 ;;
        --linux-path)         LINUX_PATH="$2"; shift 2 ;;
        --fips)               FIPS_PATH="$2"; shift 2 ;;
        --no-ping)            NO_PING=1; shift ;;
        --iperf)              DO_IPERF=1; shift ;;
        --capture)            CAPTURE=1; shift ;;
        --check-interval)     CHECK_INTERVAL="$2"; shift 2 ;;
        -v|--verbose)         VERBOSE=1; shift ;;
        -h|--help)            usage ;;
        *)                    fail "Unknown option: $1" ;;
    esac
done

# --- Detect macOS FIPS binary ---
if [ -z "$FIPS_PATH" ]; then
    if [ -x "$REPO_ROOT/target/release/fips" ]; then
        FIPS_PATH="$REPO_ROOT/target/release/fips"
    elif [ -x "$REPO_ROOT/target/debug/fips" ]; then
        FIPS_PATH="$REPO_ROOT/target/debug/fips"
    else
        fail "FIPS binary not found. Build with: cargo build --release --features ble-macos"
    fi
fi
[ -x "$FIPS_PATH" ] || fail "FIPS binary not executable: $FIPS_PATH"

if [ -z "$LINUX_PATH" ]; then
    LINUX_PATH="/home/ubuntu/src/fips/target/release/fips"
fi

LINUX_SSH="$SSH ${LINUX_USER}@${LINUX_HOST}"

TMPDIR_MAC=$(mktemp -d /tmp/fips-stability-XXXXXX)
TMPDIR_LINUX="/tmp/fips-stability-$$"
CONFIG_MAC="$TMPDIR_MAC/node-macos.yaml"
CONFIG_LINUX="$TMPDIR_LINUX/node-linux.yaml"
LOG_MAC="$TMPDIR_MAC/fips.log"
LOG_LINUX="$TMPDIR_LINUX/fips.log"
KEYLOG_MAC="$TMPDIR_MAC/keys.log"
KEYLOG_LINUX="$TMPDIR_LINUX/keys.log"
PING_LOG="$TMPDIR_MAC/ping.log"
BTMON_LOG="$TMPDIR_LINUX/btmon.log"
RESULTS_DIR="$REPO_ROOT/testing/ble/results/$(date +%Y%m%d-%H%M%S)"

SSH_PREFIX_MAC="sudo"
SSH_PREFIX_LINUX="$LINUX_SSH sudo"

cleanup() {
    local exit_code=$?
    log "Cleaning up..."
    [ -n "${MAC_PID:-}" ] && sudo kill -9 "$MAC_PID" 2>/dev/null || true
    $SSH_PREFIX_MAC pkill -9 -f 'target/release/fips' 2>/dev/null || true
    $SSH_PREFIX_MAC pkill -9 -f caffeinate 2>/dev/null || true
    $SSH_PREFIX_MAC pkill -9 -f 'ping6.*fips0' 2>/dev/null || true
    $LINUX_SSH sudo killall -9 fips btmon 2>/dev/null || true
    [ -d "$TMPDIR_MAC" ] && rm -rf "$TMPDIR_MAC"
    $LINUX_SSH "rm -rf $TMPDIR_LINUX" 2>/dev/null || true
    exit $exit_code
}
trap cleanup EXIT INT TERM HUP

# --- Preflight checks ---
log "=== BLE Stability Test ==="
log "Duration: ${DURATION} min | Rekey: ${REKEY}s | Check interval: ${CHECK_INTERVAL}s"
log "macOS FIPS: $FIPS_PATH"
log "Linux: ${LINUX_USER}@${LINUX_HOST} ($LINUX_PATH)"
echo ""

log "Preflight checks..."
$LINUX_SSH true || fail "Cannot SSH to Linux host '$LINUX_HOST'"
$LINUX_SSH test -x "$LINUX_PATH" || fail "Linux FIPS binary not found: $LINUX_PATH"
$LINUX_SSH hciconfig hci0 | grep -q "UP RUNNING" || fail "Linux BLE adapter hci0 not up"
log "  Linux: OK (hci0 up)"

if ! system_profiler SPBluetoothDataType 2>/dev/null | grep -q "Bluetooth"; then
    fail "macOS Bluetooth not available"
fi
log "  macOS: OK (Bluetooth available)"

# Check for stale FIPS processes
for host_check in "mac" "linux"; do
    if [ "$host_check" = "mac" ]; then
        stale=$(pgrep -c -f 'target/release/fips' 2>/dev/null || echo 0)
        if [ "$stale" -gt 0 ]; then
            warn "Killing $stale stale macOS FIPS processes"
            sudo pkill -9 -f 'target/release/fips' 2>/dev/null || true
            sleep 2
        fi
    else
        stale=$($LINUX_SSH "pgrep -c fips 2>/dev/null | head -1" || echo 0)
        if [ "$stale" -gt 0 ]; then
            warn "Killing $stale stale Linux FIPS processes"
            $LINUX_SSH sudo killall -9 fips 2>/dev/null || true
            sleep 2
        fi
    fi
done

# --- Write configs ---
log "Writing configs..."

cat > "$CONFIG_MAC" <<EOF
node:
  identity:
    persistent: true
  rekey:
    after_secs: ${REKEY}

tun:
  enabled: true
  name: fips0
  mtu: 1280

dns:
  enabled: true
  bind_addr: "127.0.0.1"

transports:
  ble:
    adapter: "default"
    mtu: ${DEFAULT_MTU}
    advertise: true
    scan: true
    auto_connect: true
    accept_connections: true
    send_rate_bps: ${DEFAULT_RATE_BPS}
    send_burst_bytes: ${DEFAULT_MTU}

logging:
  level: debug
EOF

$LINUX_SSH "mkdir -p $TMPDIR_LINUX"
$SCP "$CONFIG_MAC" "${LINUX_USER}@${LINUX_HOST}:${CONFIG_LINUX}" 2>/dev/null
$LINUX_SSH "sed -i 's/adapter: \"default\"/adapter: \"hci0\"/' $CONFIG_LINUX"

vlog "macOS config: $CONFIG_MAC"
vlog "Linux config: $CONFIG_LINUX"

# --- Start Linux ---
log "Starting Linux FIPS..."
if [ -n "$CAPTURE" ]; then
    $LINUX_SSH "sudo nohup btmon -w $BTMON_LOG > /dev/null 2>&1 &"
    log "  btmon capturing to $BTMON_LOG"
fi

$LINUX_SSH "cat > $TMPDIR_LINUX/start.sh" <<REMOTE_START
#!/bin/bash
export RUST_LOG=debug
export FIPS_NOISE_KEYLOG=$KEYLOG_LINUX
nohup $LINUX_PATH -c $CONFIG_LINUX > $LOG_LINUX 2>&1 </dev/null &
echo \$!
REMOTE_START
$LINUX_SSH "chmod +x $TMPDIR_LINUX/start.sh && sudo $TMPDIR_LINUX/start.sh"
log "  Waiting for BLE transport..."

for i in $(seq 1 30); do
    if $LINUX_SSH "grep -q 'transport started' $LOG_LINUX" 2>/dev/null; then
        log "  Linux BLE transport started"
        break
    fi
    if [ "$i" -eq 30 ]; then
        fail "Linux BLE transport did not start within 30s"
    fi
    sleep 1
done

# --- Start macOS ---
log "Starting macOS FIPS..."
sudo rm -f /tmp/fips-control.sock 2>/dev/null || true
sudo RUST_LOG=debug FIPS_NOISE_KEYLOG="$KEYLOG_MAC" caffeinate -i "$FIPS_PATH" -c "$CONFIG_MAC" > "$LOG_MAC" 2>&1 &
MAC_PID=$!
log "  macOS PID: $MAC_PID"

log "Waiting for handshake..."
for i in $(seq 1 60); do
    if grep -q 'promoted to active' "$LOG_MAC" 2>/dev/null; then
        log "  Handshake complete!"
        break
    fi
    if [ "$i" -eq 60 ]; then
        log "=== macOS log tail ==="
        tail -20 "$LOG_MAC"
        fail "Handshake did not complete within 60s"
    fi
    sleep 1
done

# Wait for first MMP with real metrics
sleep 5

# --- Start traffic ---
if [ -z "$NO_PING" ]; then
    log "Starting ping6 traffic..."
    sudo ping6 -i 1 -s 100 ff02::1%fips0 > "$PING_LOG" 2>&1 &
    PING_PID=$!
fi

# --- Monitor loop ---
TEST_START=$(date -u +%s)
TEST_END=$((TEST_START + DURATION * 60))
CHECK_NUM=0
PREV_REKEYS=0
PREV_PROMOTIONS=0
FAILED_CHECKS=0
CONSECUTIVE_LOSS=0

log ""
log "=== Test running: ${DURATION} minutes ==="
log "  Start:    $(date -u +%Y-%m-%dT%H:%M:%SZ)"
log "  Expected: $(date -u -r $TEST_END +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+${DURATION}M +%Y-%m-%dT%H:%M:%SZ)"
echo ""

while [ "$(date -u +%s)" -lt "$TEST_END" ]; do
    REMAINING=$(( (TEST_END - $(date -u +%s)) / 60 ))
    CHECK_NUM=$((CHECK_NUM + 1))

    # Read macOS log
    MAC_REKEYS=$(grep -c 'Rekey cutover complete' "$LOG_MAC" 2>/dev/null || true)
    MAC_PROMOTIONS=$(grep -c 'promoted to active' "$LOG_MAC" 2>/dev/null || true)
    MAC_ERRORS=$(grep -ciE 'panic|fatal' "$LOG_MAC" 2>/dev/null || true)
    MAC_WRITE_ERRS=$(grep -c 'Write Loop Error' "$LOG_MAC" 2>/dev/null || true)

    # Latest MMP from macOS
    MAC_MMP=$(grep 'MMP link metrics' "$LOG_MAC" 2>/dev/null | grep -v 'n/a.*n/a.*n/a' | tail -1 | sed 's/\x1b\[[0-9;]*m//g' || true)
    MAC_LOSS=$(echo "$MAC_MMP" | grep -oE 'loss=[0-9.]+%' | head -1 | sed 's/loss=//' || true)
    MAC_RTT=$(echo "$MAC_MMP" | grep -oE 'rtt=[0-9.]+ms' | head -1 | sed 's/rtt=//' || true)
    MAC_GOODPUT=$(echo "$MAC_MMP" | grep -oE 'goodput=[0-9]+B/s' | head -1 | sed 's/goodput=//' || true)

    # Read Linux log
    LINUX_REKEYS=$($LINUX_SSH "grep -c 'Rekey cutover complete' $LOG_LINUX" 2>/dev/null || echo "?")
    LINUX_MMP=$($LINUX_SSH "grep 'MMP link metrics' $LOG_LINUX | grep -v 'n/a.*n/a.*n/a' | tail -1" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' || true)
    LINUX_LOSS=$(echo "$LINUX_MMP" | grep -oE 'loss=[0-9.]+%' | head -1 | sed 's/loss=//' || true)
    LINUX_GOODPUT=$(echo "$LINUX_MMP" | grep -oE 'goodput=[0-9]+B/s' | head -1 | sed 's/goodput=//' || true)

    # Check macOS process alive
    if ! kill -0 "$MAC_PID" 2>/dev/null; then
        fail "macOS FIPS process died! Check $LOG_MAC"
    fi

    # Check Linux process alive
    LINUX_ALIVE=$($LINUX_SSH "pgrep -c fips" 2>/dev/null || echo 0)
    if [ "$LINUX_ALIVE" = "0" ]; then
        fail "Linux FIPS process died! Check $LOG_LINUX"
    fi

    # Memory
    MAC_RSS=$(ps -o rss= -p "$MAC_PID" 2>/dev/null | tr -d ' ' || echo "?")
    LINUX_RSS=$($LINUX_SSH "ps -o rss= -p \$(pgrep fips)" 2>/dev/null | tr -d ' ' || echo "?")

    # Delta rekeys
    MAC_REKEYS=${MAC_REKEYS:-0}
    MAC_ERRORS=${MAC_ERRORS:-0}
    MAC_WRITE_ERRS=${MAC_WRITE_ERRS:-0}
    DELTA_REKEYS=$((MAC_REKEYS - PREV_REKEYS))
    PREV_REKEYS=$MAC_REKEYS

    # Loss check (integer-only: compare loss * 10 against 50 for 5.0% threshold)
    LOSS_NUM=$(echo "${MAC_LOSS:-0}" | sed 's/%//' 2>/dev/null || echo "0")
    LOSS_INT=$(echo "$LOSS_NUM" | awk -F. '{printf "%d", $1 * 10 + ($2 + 0) / 1 + 0}' 2>/dev/null || echo "0")
    if [ "$LOSS_INT" -gt 50 ]; then
        CONSECUTIVE_LOSS=$((CONSECUTIVE_LOSS + 1))
        warn "High loss detected: $MAC_LOSS (consecutive: $CONSECUTIVE_LOSS)"
    else
        CONSECUTIVE_LOSS=0
    fi

    echo "=== Check #${CHECK_NUM} (~${REMAINING}m remaining) ==="
    echo "  macOS: loss=$MAC_LOSS rtt=$MAC_RTT goodput=$MAC_GOODPUT rekeys=$MAC_REKEYS(+${DELTA_REKEYS}) rss=${MAC_RSS}KB"
    echo "  linux: loss=$LINUX_LOSS goodput=$LINUX_GOODPUT rekeys=$LINUX_REKEYS rss=${LINUX_RSS}KB"
    echo "  errs: panics=$MAC_ERRORS write_errs=$MAC_WRITE_ERRS"

    # Alert on problems
    if [ "$MAC_ERRORS" -gt 0 ]; then
        warn "Panics or fatal errors detected in macOS log!"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
    fi

    if [ "$CONSECUTIVE_LOSS" -ge 3 ]; then
        warn "Loss >5% for 3+ consecutive checks"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
    fi

    # Sleep until next check
    sleep "$CHECK_INTERVAL"
done

TEST_END_ACTUAL=$(date -u +%s)
TEST_DURATION_SECS=$((TEST_END_ACTUAL - TEST_START))

# --- Final report ---
log ""
log "========================================="
log "=== BLE Stability Test Report ==="
log "========================================="
echo ""
echo "Duration: $((TEST_DURATION_SECS / 60))m $((TEST_DURATION_SECS % 60))s"
echo "Start:    $(date -u -r $TEST_START +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "End:      $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

MAC_FINAL_REKEYS=$(grep -c 'Rekey cutover complete' "$LOG_MAC" 2>/dev/null || true)
MAC_FINAL_PROMOTIONS=$(grep -c 'promoted to active' "$LOG_MAC" 2>/dev/null || true)
MAC_FINAL_ERRORS=$(grep -ciE 'panic|fatal' "$LOG_MAC" 2>/dev/null || true)
MAC_FINAL_WRITE_ERRS=$(grep -c 'Write Loop Error' "$LOG_MAC" 2>/dev/null || true)
MAC_DISCONNECTS=$(grep -c 'Disconnect' "$LOG_MAC" 2>/dev/null || true)

LINUX_FINAL_REKEYS=$($LINUX_SSH "grep -c 'Rekey cutover complete' $LOG_LINUX" 2>/dev/null || echo "?")

# All MMP loss values
MAC_ALL_LOSS=$(grep 'MMP link metrics' "$LOG_MAC" 2>/dev/null | grep -v 'n/a.*n/a.*n/a' | sed 's/\x1b\[[0-9;]*m//g' | grep -oE 'loss=[0-9.]+%' | sed 's/loss=//' | sort -n)
MAC_MAX_LOSS=$(echo "$MAC_ALL_LOSS" | tail -1)
MAC_MIN_LOSS=$(echo "$MAC_ALL_LOSS" | head -1)
MAC_LOSS_SAMPLES=$(echo "$MAC_ALL_LOSS" | wc -l | tr -d ' ')

# Goodput range
MAC_ALL_GOODPUT=$(grep 'MMP link metrics' "$LOG_MAC" 2>/dev/null | grep -v 'n/a.*n/a.*n/a' | sed 's/\x1b\[[0-9;]*m//g' | grep -oE 'goodput=[0-9]+B/s' | sed 's/goodput=//' | sort -n)
MAC_MIN_GOODPUT=$(echo "$MAC_ALL_GOODPUT" | head -1)
MAC_MAX_GOODPUT=$(echo "$MAC_ALL_GOODPUT" | tail -1)

# RTT range
MAC_ALL_RTT=$(grep 'MMP link metrics' "$LOG_MAC" 2>/dev/null | grep -v 'n/a.*n/a.*n/a' | sed 's/\x1b\[[0-9;]*m//g' | grep -oE 'rtt=[0-9.]+ms' | sed 's/rtt=//' | sort -n)
MAC_MIN_RTT=$(echo "$MAC_ALL_RTT" | head -1)
MAC_MAX_RTT=$(echo "$MAC_ALL_RTT" | tail -1)

echo "| Metric             | Value                      |"
echo "|--------------------|----------------------------|"
echo "| Duration           | $((TEST_DURATION_SECS / 60))m $((TEST_DURATION_SECS % 60))s                    |"
echo "| MMP loss samples   | $MAC_LOSS_SAMPLES                          |"
echo "| MMP loss range     | ${MAC_MIN_LOSS} – ${MAC_MAX_LOSS}                   |"
echo "| RTT range          | ${MAC_MIN_RTT} – ${MAC_MAX_RTT}              |"
echo "| Goodput range      | ${MAC_MIN_GOODPUT} – ${MAC_MAX_GOODPUT}             |"
echo "| Rekeys (macOS)     | $MAC_FINAL_REKEYS                          |"
echo "| Rekeys (Linux)     | $LINUX_FINAL_REKEYS                          |"
echo "| Promotions         | $MAC_FINAL_PROMOTIONS                          |"
echo "| Disconnects        | $MAC_DISCONNECTS                          |"
echo "| Write Loop Errors  | $MAC_FINAL_WRITE_ERRS                          |"
echo "| Panics/Fatals      | $MAC_FINAL_ERRORS                          |"
echo "| Failed checks      | $FAILED_CHECKS                          |"
echo ""

# Memory at end
MAC_FINAL_RSS=$(ps -o rss= -p "$MAC_PID" 2>/dev/null | tr -d ' ' || echo "?")
LINUX_FINAL_RSS=$($LINUX_SSH "ps -o rss= -p \$(pgrep fips)" 2>/dev/null | tr -d ' ' || echo "?")
echo "Memory: macOS=${MAC_FINAL_RSS}KB Linux=${LINUX_FINAL_RSS}KB"
echo ""

# --- Verdict ---
VERDICT="PASS"
MAC_FINAL_ERRORS=${MAC_FINAL_ERRORS:-0}
if [ "$MAC_FINAL_ERRORS" -gt 0 ]; then
    VERDICT="FAIL (panics/fatal errors)"
elif [ "$FAILED_CHECKS" -gt 0 ]; then
    VERDICT="DEGRADED ($FAILED_CHECKS failed checks)"
elif [ "$MAC_LOSS_SAMPLES" -lt 5 ]; then
    VERDICT="INSUFFICIENT DATA (only $MAC_LOSS_SAMPLES MMP samples)"
fi

echo "Verdict: $VERDICT"
echo ""

# --- Save results ---
mkdir -p "$RESULTS_DIR"
cp "$LOG_MAC" "$RESULTS_DIR/macos-fips.log" 2>/dev/null || true
cp "$CONFIG_MAC" "$RESULTS_DIR/macos-config.yaml" 2>/dev/null || true
[ -f "$PING_LOG" ] && cp "$PING_LOG" "$RESULTS_DIR/ping.log" 2>/dev/null || true
$LINUX_SSH "cat $LOG_LINUX" > "$RESULTS_DIR/linux-fips.log" 2>/dev/null || true
$LINUX_SSH "cat $CONFIG_LINUX" > "$RESULTS_DIR/linux-config.yaml" 2>/dev/null || true
if [ -n "$CAPTURE" ]; then
    $LINUX_SSH "cat $BTMON_LOG" > "$RESULTS_DIR/btmon.log" 2>/dev/null || true
fi

$LINUX_SSH "cat $KEYLOG_LINUX" > "$RESULTS_DIR/linux-keys.log" 2>/dev/null || true
cp "$KEYLOG_MAC" "$RESULTS_DIR/macos-keys.log" 2>/dev/null || true

# Write summary
cat > "$RESULTS_DIR/summary.txt" <<EOF
BLE Stability Test — $(date -u +%Y-%m-%dT%H:%M:%SZ)
Duration: ${DURATION} min | Rekey: ${REKEY}s | Check interval: ${CHECK_INTERVAL}s
macOS: $(uname -m) | Linux: ${LINUX_USER}@${LINUX_HOST}
Verdict: $VERDICT

MMP loss range: ${MAC_MIN_LOSS} – ${MAC_MAX_LOSS} (${MAC_LOSS_SAMPLES} samples)
RTT range: ${MAC_MIN_RTT} – ${MAC_MAX_RTT}
Goodput range: ${MAC_MIN_GOODPUT} – ${MAC_MAX_GOODPUT}
Rekeys: macOS=$MAC_FINAL_REKEYS linux=$LINUX_FINAL_REKEYS
Promotions: $MAC_FINAL_PROMOTIONS
Disconnects: $MAC_DISCONNECTS
Write Loop Errors: $MAC_FINAL_WRITE_ERRS
Panics/Fatals: $MAC_FINAL_ERRORS
Memory: macOS=${MAC_FINAL_RSS}KB Linux=${LINUX_FINAL_RSS}KB
EOF

log "Results saved to $RESULTS_DIR/"

# --- Optional: iperf3 ---
if [ -n "$DO_IPERF" ]; then
    log ""
    log "=== iperf3 throughput test ==="

    LINUX_IPV6_RAW=$($LINUX_SSH "ip -6 addr show fips0 scope global 2>/dev/null | grep -oP 'inet6 \K[^/]+'" 2>/dev/null || echo "")
    if [ -z "$LINUX_IPV6_RAW" ]; then
        warn "Cannot find Linux IPv6 on fips0, trying control socket..."
        LINUX_IPV6_RAW=$(sudo cat /tmp/fips-control.sock 2>/dev/null | head -1 || echo "")
    fi

    if [ -n "$LINUX_IPV6_RAW" ]; then
        LINUX_IPV6=$(echo "$LINUX_IPV6_RAW" | tr -d '[:space:]')
        log "Linux IPv6: $LINUX_IPV6"

        $LINUX_SSH "command -v iperf3" >/dev/null 2>&1 || warn "iperf3 not found on Linux"
        command -v iperf3 >/dev/null 2>&1 || warn "iperf3 not found on macOS"

        if $LINUX_SSH "command -v iperf3" >/dev/null 2>&1 && command -v iperf3 >/dev/null 2>&1; then
            log "Starting iperf3 server on Linux..."
            $LINUX_SSH "sudo killall -9 iperf3 2>/dev/null; iperf3 -s --daemon"

            sleep 2

            MAC_IPV6=$(ifconfig fips0 2>/dev/null | grep 'inet6 fd' | awk '{print $2}' | head -1 || true)

            IPERF_BIND=""
            [ -n "$MAC_IPV6" ] && IPERF_BIND="-B $MAC_IPV6"

            # TCP default (10s) — expected burst-stall over BLE
            log "Running iperf3 TCP (default socket buffer, 10s)..."
            iperf3 -c "$LINUX_IPV6" $IPERF_BIND -t 10 -P 1 2>&1 | tee "$RESULTS_DIR/iperf3-tcp-default.txt" || warn "iperf3 TCP default failed"

            sleep 2

            # TCP with 8KB socket buffer (30s) — matches TCP window clamp, should sustain
            log "Running iperf3 TCP (-w 8K socket buffer, 30s)..."
            iperf3 -c "$LINUX_IPV6" $IPERF_BIND -t 30 -w 8K -P 1 2>&1 | tee "$RESULTS_DIR/iperf3-tcp-8k.txt" || warn "iperf3 TCP -w 8K failed"

            sleep 2

            # UDP at BLE-appropriate rate (50 Kbps, below 80 Kbps AIMD ceiling)
            log "Running iperf3 UDP (50 Kbps, 10s)..."
            iperf3 -c "$LINUX_IPV6" $IPERF_BIND -t 10 -u -b 50K -P 1 2>&1 | tee "$RESULTS_DIR/iperf3-udp-50K.txt" || warn "iperf3 UDP 50K failed"

            sleep 2

            # UDP above BLE ceiling (100 Kbps)
            log "Running iperf3 UDP (100 Kbps, 10s)..."
            iperf3 -c "$LINUX_IPV6" $IPERF_BIND -t 10 -u -b 100K -P 1 2>&1 | tee "$RESULTS_DIR/iperf3-udp-100K.txt" || warn "iperf3 UDP 100K failed"

            $LINUX_SSH "sudo killall -9 iperf3 2>/dev/null" || true
        else
            warn "iperf3 not available on both hosts, skipping"
        fi
    else
        warn "Could not determine Linux IPv6 address, skipping iperf3"
    fi

    # --- SSH over FIPS mesh ---
    log ""
    log "=== SSH over FIPS mesh ==="

    if [ -n "$LINUX_IPV6" ]; then
        # Re-read IPv6 in case iperf3 section was skipped
        if [ -z "$LINUX_IPV6_RAW" ]; then
            LINUX_IPV6=$($LINUX_SSH "ip -6 addr show fips0 scope global 2>/dev/null | grep -oP 'inet6 \K[^/]+'" 2>/dev/null | tr -d '[:space:]' || echo "")
        fi

        if [ -n "$LINUX_IPV6" ]; then
            log "Testing SSH to ${LINUX_USER}@${LINUX_IPV6}..."
            SSH_START=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time()*1e9))")
            SSH_OUTPUT=$(ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
                "${LINUX_USER}@${LINUX_IPV6}" "uname -a && uptime" 2>&1)
            SSH_RC=$?
            SSH_END=$(date +%s%N 2>/dev/null || python3 -c "import time; print(int(time.time()*1e9))")
            SSH_MS=$(( (SSH_END - SSH_START) / 1000000 ))

            if [ $SSH_RC -eq 0 ]; then
                log "SSH OK (${SSH_MS}ms): $(echo "$SSH_OUTPUT" | head -1)"
                echo "SSH: OK (${SSH_MS}ms)" >> "$RESULTS_DIR/summary.txt"
                echo "$SSH_OUTPUT" > "$RESULTS_DIR/ssh-test.txt"

                # Small data transfer via SSH
                log "Testing data transfer via SSH (5KB)..."
                DD_OUTPUT=$(ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
                    "${LINUX_USER}@${LINUX_IPV6}" "dd if=/dev/urandom bs=1024 count=5 2>/dev/null | wc -c" 2>&1)
                DD_RC=$?
                if [ $DD_RC -eq 0 ]; then
                    log "SSH data transfer OK: ${DD_OUTPUT} bytes received"
                    echo "SSH data transfer: OK (${DD_OUTPUT} bytes)" >> "$RESULTS_DIR/summary.txt"
                else
                    warn "SSH data transfer failed: $DD_OUTPUT"
                    echo "SSH data transfer: FAILED" >> "$RESULTS_DIR/summary.txt"
                fi
            else
                warn "SSH failed (${SSH_MS}ms): $SSH_OUTPUT"
                echo "SSH: FAILED (${SSH_MS}ms)" >> "$RESULTS_DIR/summary.txt"
            fi
        else
            warn "Cannot determine Linux IPv6 for SSH test"
            echo "SSH: SKIPPED (no IPv6)" >> "$RESULTS_DIR/summary.txt"
        fi
    else
        warn "Linux IPv6 not available for SSH test"
        echo "SSH: SKIPPED (no IPv6)" >> "$RESULTS_DIR/summary.txt"
    fi
fi

log ""
log "=== Done ==="

if [[ "$VERDICT" == PASS ]]; then
    exit 0
else
    exit 1
fi
