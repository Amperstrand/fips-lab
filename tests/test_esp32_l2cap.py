"""ESP32 BLE L2CAP structured regression tests.

Mode-agnostic: works with labgrid (--lg-env) or standalone (SSH fallback).

With labgrid:
    pytest --lg-env=environment.yaml tests/test_esp32_l2cap.py -v

Standalone (SSH):
    pytest tests/test_esp32_l2cap.py -v

Tests use `esp32` and `fips` fixtures from conftest.py which automatically
select labgrid drivers or SSH adapters based on availability.
"""

import time

import pytest

HANDSHAKE_TIMEOUT = 60
HEARTBEAT_SETTLE = 30
RECONNECT_TIMEOUT = 120
STABILITY_DURATION = 300


@pytest.fixture(scope="module", autouse=True)
def fips_running(fips):
    fips.restart()
    time.sleep(3)
    yield


class TestHandshake:

    def test_handshake_completes(self, esp32):
        output = esp32.reset_and_capture(HANDSHAKE_TIMEOUT)
        assert "handshake ok" in output, (
            f"IK handshake did not complete within {HANDSHAKE_TIMEOUT}s"
        )

    def test_no_invalid_message(self, esp32):
        output = esp32.reset_and_capture(HANDSHAKE_TIMEOUT)
        assert "InvalidMessage" not in output, "Noise pattern mismatch"


class TestHeartbeats:

    def test_heartbeats_flow(self, esp32):
        esp32.reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(HEARTBEAT_SETTLE + 30)
        stats = esp32.show_stats()
        assert stats.get("hb_tx", 0) > 0, "No heartbeats sent"
        assert stats.get("hb_rx", 0) > 0, "No heartbeats received"

    def test_heartbeat_rate(self, esp32):
        esp32.reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(120)
        stats = esp32.show_stats()
        assert stats.get("hb_tx", 0) >= 8, (
            f"Heartbeat rate too low: {stats.get('hb_tx', 0)} in ~2min"
        )


class TestFipsRestartRecovery:

    def test_reconnect_after_restart(self, esp32, fips):
        esp32.reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(HEARTBEAT_SETTLE)
        stats_before = esp32.show_stats()
        msg2_before = stats_before.get("msg2_rx", 0)

        fips.restart()

        deadline = time.monotonic() + RECONNECT_TIMEOUT
        reconnected = False
        while time.monotonic() < deadline:
            time.sleep(10)
            stats = esp32.show_stats()
            if stats.get("msg2_rx", 0) > msg2_before:
                reconnected = True
                break

        assert reconnected, f"ESP32 did not reconnect within {RECONNECT_TIMEOUT}s"

    def test_heartbeats_resume_after_restart(self, esp32, fips):
        esp32.reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(HEARTBEAT_SETTLE)
        hb_before = esp32.show_stats().get("hb_tx", 0)

        fips.restart()
        time.sleep(RECONNECT_TIMEOUT)

        hb_after = esp32.show_stats().get("hb_tx", 0)
        assert hb_after > hb_before, "Heartbeats did not resume after restart"


class TestStability:

    @pytest.mark.slow
    def test_no_drops_5min(self, esp32):
        esp32.reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(HEARTBEAT_SETTLE)
        start = esp32.show_stats()

        time.sleep(STABILITY_DURATION)

        end = esp32.show_stats()
        new_drops = end.get("l2cap_rx_drops", 0) - start.get("l2cap_rx_drops", 0)
        new_timeouts = end.get("l2cap_recv_timeouts", 0) - start.get("l2cap_recv_timeouts", 0)

        assert new_drops == 0, f"{new_drops} drops in {STABILITY_DURATION}s"
        assert new_timeouts == 0, f"{new_timeouts} timeouts in {STABILITY_DURATION}s"
        assert end.get("hb_tx", 0) > start.get("hb_tx", 0), "No heartbeats during stability window"
