#!/usr/bin/env bash
# Dry validation of Phase 2 deployment (hardware-absent branch)
# This script validates configs without requiring physical hardware

set -euo pipefail

EVIDENCE_DIR="/home/ubuntu/src/.omo/evidence"
EVIDENCE_FILE="$EVIDENCE_DIR/task-21-amperstrand-nfc-mcu-dedup.txt"
mkdir -p "$EVIDENCE_DIR"

echo "===== Phase 2 Dry Validation =====" | tee -a "$EVIDENCE_FILE"
echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "$EVIDENCE_FILE"
echo "" | tee -a "$EVIDENCE_FILE"

# 1. Verify coordinator liveness
echo "[1/5] Verifying coordinator liveness on :20408..." | tee -a "$EVIDENCE_FILE"
if ss -tlnp | grep -q ":20408"; then
    echo "  ✓ Coordinator is running" | tee -a "$EVIDENCE_FILE"
    echo "    PID: $(ps aux | grep labgrid-coordinator | grep -v grep | awk '{print $2}')" | tee -a "$EVIDENCE_FILE"
else
    echo "  ✗ Coordinator NOT running" | tee -a "$EVIDENCE_FILE"
fi
echo "" | tee -a "$EVIDENCE_FILE"

# 2. Validate coordinator config syntax
echo "[2/5] Validating environment-coordinator.yaml..." | tee -a "$EVIDENCE_FILE"
if python3 -c "import yaml; yaml.safe_load(open('/home/ubuntu/src/fips-lab/config/environment-coordinator.yaml'))"; then
    echo "  ✓ Config is valid YAML" | tee -a "$EVIDENCE_FILE"
else
    echo "  ✗ Config is invalid YAML" | tee -a "$EVIDENCE_FILE"
fi
echo "" | tee -a "$EVIDENCE_FILE"

# 3. Validate exporter config syntax
echo "[3/5] Validating exporter config syntax..." | tee -a "$EVIDENCE_FILE"
for cfg in /home/ubuntu/src/fips-lab/config/exporter-*.yaml; do
    if [[ -f "$cfg" ]]; then
        cfg_name=$(basename "$cfg")
        if python3 -c "import yaml; yaml.safe_load(open('$cfg'))"; then
            echo "  ✓ $cfg_name is valid YAML" | tee -a "$EVIDENCE_FILE"
        else
            echo "  ✗ $cfg_name is invalid YAML" | tee -a "$EVIDENCE_FILE"
        fi
    fi
done
echo "" | tee -a "$EVIDENCE_FILE"

# 4. Check systemd unit files
echo "[4/5] Validating systemd unit files..." | tee -a "$EVIDENCE_FILE"
for unit in /home/ubuntu/src/fips-lab/config/systemd/*.service; do
    if [[ -f "$unit" ]]; then
        unit_name=$(basename "$unit")
        if systemd-analyze verify "$unit" 2>/dev/null; then
            echo "  ✓ $unit_name is valid" | tee -a "$EVIDENCE_FILE"
        else
            echo "  ✗ $unit_name has errors" | tee -a "$EVIDENCE_FILE"
        fi
    fi
done
echo "" | tee -a "$EVIDENCE_FILE"

# 5. Hardware availability check
echo "[5/5] Checking hardware availability..." | tee -a "$EVIDENCE_FILE"
if ping -c 1 -W 2 192.168.13.218 >/dev/null 2>&1; then
    echo "  ✓ 218 (Linux host) is reachable" | tee -a "$EVIDENCE_FILE"
    HARDWARE_AVAILABLE="true"
else
    echo "  ✗ 218 (Linux host) NOT reachable" | tee -a "$EVIDENCE_FILE"
    HARDWARE_AVAILABLE="false"
fi

# Check for local ESP32 devices
if lsusb | grep -qi "espressif"; then
    echo "  ✓ ESP32 device(s) found on local machine" | tee -a "$EVIDENCE_FILE"
    lsusb | grep -i espressif | tee -a "$EVIDENCE_FILE"
else
    echo "  ⚠ No ESP32 devices found on local machine" | tee -a "$EVIDENCE_FILE"
fi
echo "" | tee -a "$EVIDENCE_FILE"

# Summary
echo "===== Summary =====" | tee -a "$EVIDENCE_FILE"
if [[ "$HARDWARE_AVAILABLE" == "true" ]]; then
    echo "Hardware status: AVAILABLE (full deployment possible)" | tee -a "$EVIDENCE_FILE"
else
    echo "Hardware status: ABSENT (218 unreachable)" | tee -a "$EVIDENCE_FILE"
    echo "Action: Pilot test will be skipped with honest documentation" | tee -a "$EVIDENCE_FILE"
fi
echo "" | tee -a "$EVIDENCE_FILE"
echo "Evidence written to: $EVIDENCE_FILE" | tee -a "$EVIDENCE_FILE"