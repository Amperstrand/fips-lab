from conftest import ESP32_NPUB

import pytest


@pytest.mark.xfail(reason="ESP32 BLE L2CAP cross-connection instability, issue #133", strict=False)
def test_echo_esp32_small_payload(linux_target, with_esp32_peer):
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_echo(ESP32_NPUB, count=5, payload_size=0)
    assert result.get("status") != "error", f"Benchmark failed: {result}"
    if result.get("loss_count") is not None:
        assert result["loss_count"] == 0, (
            f"Echo loss: {result['loss_count']}/{result.get('count', '?')}"
        )
    if result.get("median_us") is not None:
        median_ms = result["median_us"] / 1000
        assert median_ms < 500, f"Median RTT too high: {median_ms}ms"
