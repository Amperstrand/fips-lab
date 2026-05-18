#!/usr/bin/env bash
set -euo pipefail

# Deploy labgrid Phase 2 to 218 (Linux host)
#
# Prerequisites:
#   - 218 is reachable (ping 192.168.13.218)
#   - SSH key auth configured (Host 218 in ~/.ssh/config)
#   - FIPS already built on 218 (~/src/fips/target/release/fips)
#
# Usage: ./scripts/setup-218-phase2.sh [--dry-run]

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

run() {
  if $DRY_RUN; then
    echo "  [dry-run] $*"
  else
    echo "  $ $*"
    ssh 218 "$@"
  fi
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$REPO_DIR/config"

echo "==> Phase 2 deployment to 218"

echo "==> [1/5] Verifying connectivity"
if ! $DRY_RUN; then
  if ! ping -c 1 -W 2 192.168.13.218 > /dev/null 2>&1; then
    echo "ERROR: 218 unreachable" >&2
    exit 1
  fi
  echo "  OK — 218 reachable"
else
  echo "  (skipped — dry-run)"
fi

echo "==> [2/5] Checking prerequisites"
run "test -f /home/ubuntu/src/fips/target/release/fips" || {
  echo "ERROR: FIPS binary not found on 218" >&2
  exit 1
}
run "test -f /etc/fips/fips.yaml" || {
  echo "WARNING: FIPS config not found at /etc/fips/fips.yaml"
}
echo "  OK — FIPS binary and config present"

echo "==> [3/5] Installing labgrid exporter config"
run "mkdir -p /home/ubuntu/src/fips-lab/config"
if ! $DRY_RUN; then
  scp "$CONFIG_DIR/exporter-218.yaml" 218:/home/ubuntu/src/fips-lab/config/
fi
echo "  OK — exporter-218.yaml deployed"

echo "==> [4/5] Installing systemd units"
run "mkdir -p /tmp/fips-lab-systemd"
if ! $DRY_RUN; then
  scp "$CONFIG_DIR/systemd/fips.service" 218:/tmp/fips-lab-systemd/
  scp "$CONFIG_DIR/systemd/labgrid-exporter.service" 218:/tmp/fips-lab-systemd/
  ssh 218 "sudo cp /tmp/fips-lab-systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload"
fi
echo "  OK — systemd units installed"

echo "==> [5/5] Enabling services"
run "sudo systemctl enable fips.service"
run "sudo systemctl enable labgrid-exporter.service"
if ! $DRY_RUN; then
  echo ""
  echo "  Services ready. Start with:"
  echo "    ssh 218 'sudo systemctl start fips'"
  echo "    ssh 218 'sudo systemctl start labgrid-exporter'"
fi

echo ""
echo "==> Phase 2 setup complete!"
if $DRY_RUN; then
  echo "  (dry-run — no changes made)"
fi
