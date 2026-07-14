"""ESP32 BLE L2CAP structured regression tests.

Tests the microfips ESP32 firmware against the FIPS daemon on ai-legion-small.
Coordinates two hosts:
  - ai-legion (192.168.13.208): ESP32-D0WD serial port
  - ai-legion-small: FIPS daemon with BLE adapter

Run:
    pytest --lg-env=environment.yaml tests/test_esp32_l2cap.py -v

Or standalone (without labgrid):
    pytest tests/test_esp32_l2cap.py -v
"""

import json
import subprocess
import time

import pytest

SSH_LEGION = [
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
    "ubuntu@ai-legion",
]
SSH_SMALL = [
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
    "ubuntu@ai-legion-small",
]

ESP32_NPUB = "npub1ccz8l9zpt8lk4v3mfgqxq8wslhwpyrwg6wllqfxf3vqzh2gslwms5xdv4c"
HANDSHAKE_TIMEOUT = 60
HEARTBEAT_TIMEOUT = 90
RECONNECT_TIMEOUT = 120
STABILITY_DURATION = 300  # 5 minutes


def _ssh(host_cmd, target="legion"):
    ssh_cmd = SSH_LEGION if target == "legion" else SSH_SMALL
    result = subprocess.run(
        ssh_cmd + [host_cmd],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


def _esp32_show_stats():
    out = _ssh(
        'sudo python3 -c "'
        'import serial,time,json;'
        "s=serial.Serial('/dev/ttyUSB0',115200,timeout=1);"
        "s.write(b'show_stats\\n');"
        "time.sleep(1.5);"
        "d=s.read(4096);s.close();"
        "print(d.decode(errors='replace'))"
        '"',
        target="legion",
    )
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)["data"]
            except Exception:
                pass
    return {}


def _esp32_reset_and_capture(duration_s):
    out = _ssh(
        f'sudo python3 -c "'
        "import serial,time;"
        "s=serial.Serial('/dev/ttyUSB0',115200,timeout=0.1);"
        "s.dtr=False;s.rts=True;time.sleep(0.1);"
        "s.dtr=True;s.rts=True;time.sleep(0.05);"
        "s.dtr=False;s.rts=False;time.sleep(0.2);"
        f"start=time.time();buf='';"
        f"while time.time()-start<{duration_s}:"
        "d=s.read(4096);"
        "buf+=d.decode(errors='replace') if d else '';"
        "s.close();print(buf)"
        '"',
        target="legion",
    )
    return out


def _restart_fips():
    _ssh("sudo systemctl restart fips", target="small")
    time.sleep(3)


@pytest.fixture(scope="module", autouse=True)
def fips_running():
    _restart_fips()
    yield


class TestHandshake:
    """Verify Noise IK handshake completes between FIPS daemon and ESP32."""

    def test_handshake_completes(self):
        serial_output = _esp32_reset_and_capture(HANDSHAKE_TIMEOUT)
        assert "handshake ok" in serial_output, (
            f"IK handshake did not complete within {HANDSHAKE_TIMEOUT}s. "
            f"Serial output:\n{serial_output[-500:]}"
        )

    def test_no_invalid_message(self):
        serial_output = _esp32_reset_and_capture(HANDSHAKE_TIMEOUT)
        assert "InvalidMessage" not in serial_output, (
            "Handshake produced InvalidMessage — Noise pattern mismatch"
        )


class TestHeartbeats:
    """Verify bidirectional heartbeat flow after handshake."""

    def test_heartbeats_flow(self):
        _esp32_reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(HEARTBEAT_TIMEOUT - HANDSHAKE_TIMEOUT + 10)
        stats = _esp32_show_stats()
        assert stats.get("hb_tx", 0) > 0, "ESP32 did not send any heartbeats"
        assert stats.get("hb_rx", 0) > 0, "ESP32 did not receive any heartbeats"

    def test_heartbeat_rate(self):
        _esp32_reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(120)
        stats = _esp32_show_stats()
        hb_tx = stats.get("hb_tx", 0)
        expected_min = 8
        assert hb_tx >= expected_min, (
            f"Heartbeat rate too low: {hb_tx} in ~2min, expected >= {expected_min}"
        )


class TestFipsRestartRecovery:
    """Verify ESP32 automatically reconnects after FIPS daemon restart."""

    def test_reconnect_after_restart(self):
        _esp32_reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(30)
        stats_before = _esp32_show_stats()
        msg2_before = stats_before.get("msg2_rx", 0)

        _restart_fips()

        deadline = time.monotonic() + RECONNECT_TIMEOUT
        reconnected = False
        while time.monotonic() < deadline:
            time.sleep(10)
            stats = _esp32_show_stats()
            if stats.get("msg2_rx", 0) > msg2_before:
                reconnected = True
                break

        assert reconnected, (
            f"ESP32 did not reconnect within {RECONNECT_TIMEOUT}s after FIPS restart"
        )

    def test_heartbeats_resume_after_restart(self):
        _esp32_reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(30)
        stats_before = _esp32_show_stats()
        hb_before = stats_before.get("hb_tx", 0)

        _restart_fips()
        time.sleep(RECONNECT_TIMEOUT)

        stats_after = _esp32_show_stats()
        assert stats_after.get("hb_tx", 0) > hb_before, (
            "Heartbeats did not resume after FIPS restart recovery"
        )


class TestStability:
    """Verify link stability over a 5-minute window."""

    @pytest.mark.slow
    def test_no_drops_5min(self):
        _esp32_reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(30)
        stats_start = _esp32_show_stats()
        drops_start = stats_start.get("l2cap_rx_drops", 0)
        timeouts_start = stats_start.get("l2cap_recv_timeouts", 0)

        time.sleep(STABILITY_DURATION)

        stats_end = _esp32_show_stats()
        drops_end = stats_end.get("l2cap_rx_drops", 0)
        timeouts_end = stats_end.get("l2cap_recv_timeouts", 0)

        new_drops = drops_end - drops_start
        new_timeouts = timeouts_end - timeouts_start

        assert new_drops == 0, f"{new_drops} new drops during {STABILITY_DURATION}s stability window"
        assert new_timeouts == 0, f"{new_timeouts} new timeouts during {STABILITY_DURATION}s stability window"
        assert stats_end.get("hb_tx", 0) > stats_start.get("hb_tx", 0), "No heartbeats during stability window"
