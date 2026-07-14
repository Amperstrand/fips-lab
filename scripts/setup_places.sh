#!/usr/bin/env bash
# Create labgrid places on the coordinator for all devices.
# Run once after deploying exporters.
#
# Usage: bash scripts/setup_places.sh
#
# Prerequisites:
#   - labgrid-coordinator running on ai-legion-small:20408
#   - exporters running on ai-legion and ai-legion-small

set -euo pipefail

COORDINATOR="${LG_COORDINATOR:-ai-legion-small:20408}"
CLIENT="labgrid-client -c $COORDINATOR"

echo "=== Creating labgrid places ==="

# ESP32-D0WD on ai-legion
$CLIENT add-place esp32-d0wd 2>/dev/null || true
$CLIENT add-match esp32-d0wd 'ai-legion/esp32-d0wd/USBSerialPort/*' 2>/dev/null || true
$CLIENT add-alias esp32-d0wd esp32 2>/dev/null || true
echo "  ✓ esp32-d0wd (serial port on ai-legion)"

# FIPS daemon on ai-legion-small
$CLIENT add-place fips-daemon 2>/dev/null || true
$CLIENT add-match fips-daemon 'ai-legion-small/fips-daemon/NetworkService/*' 2>/dev/null || true
$CLIENT add-alias fips-daemon fips 2>/dev/null || true
echo "  ✓ fips-daemon (FIPS service on ai-legion-small)"

# ESP8266 on ai-legion-small (future use)
$CLIENT add-place esp8266 2>/dev/null || true
$CLIENT add-match esp8266 'ai-legion-small/esp8266/USBSerialPort/*' 2>/dev/null || true
echo "  ✓ esp8266 (serial port on ai-legion-small)"

echo ""
echo "=== Places created. Verify with: ==="
echo "  labgrid-client -c $COORDINATOR resources"
echo "  labgrid-client -c $COORDINATOR places"
