"""BLE performance sweep and stability scenarios.

Sweeps rate x frame_size to map viable BLE throughput parameter space,
runs stability soaks, and tests reconnection timing.

Run:
    pytest --lg-env=environment.yaml tests/test_scenarios.py -v
"""

import time

import pytest

from conftest import LINUX_NPUB, MAC_NPUB

SWEEP_RATES = [10000, 20000, 30000, 40000, 50000]
SWEEP_FRAME_SIZES = [20, 50, 100]
SWEEP_DURATION = 3

SOAK_DURATION = 60
SOAK_PAYLOAD_SIZE = 64
SOAK_COUNT = 200
SOAK_MAX_LOSS_RATE = 0.05

RECONNECT_TIMEOUT = 120


@pytest.mark.benchmark
@pytest.mark.parametrize("rate", SWEEP_RATES, ids=lambda r: f"r{r//1000}k")
@pytest.mark.parametrize("frame_size", SWEEP_FRAME_SIZES, ids=lambda f: f"fs{f}")
def test_throughput_sweep_linux_to_mac(
    linux_target, benchmark_results, rate, frame_size, with_mac_peer,
):
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_throughput(
        MAC_NPUB,
        direction="upload",
        duration=SWEEP_DURATION,
        frame_size=frame_size,
        rate=rate,
    )
    status = result.get("status", "unknown")
    entry = {
        "test": "throughput_sweep",
        "pair": "linux->mac",
        "direction": "upload",
        "rate": rate,
        "frame_size": frame_size,
        "status": status,
    }
    if status != "error":
        entry["achieved_bps"] = result.get("achieved_bps")
        entry["loss_rate"] = result.get("loss_rate")
    benchmark_results.append(entry)
    if status == "error":
        pytest.skip(f"BLE queue full at rate={rate} frame_size={frame_size}")


@pytest.mark.benchmark
@pytest.mark.parametrize("rate", SWEEP_RATES, ids=lambda r: f"r{r//1000}k")
@pytest.mark.parametrize("frame_size", SWEEP_FRAME_SIZES, ids=lambda f: f"fs{f}")
def test_throughput_sweep_mac_to_linux(
    mac_target, benchmark_results, rate, frame_size, with_linux_peer,
):
    fipsctl = mac_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_throughput(
        LINUX_NPUB,
        direction="upload",
        duration=SWEEP_DURATION,
        frame_size=frame_size,
        rate=rate,
    )
    status = result.get("status", "unknown")
    entry = {
        "test": "throughput_sweep",
        "pair": "mac->linux",
        "direction": "upload",
        "rate": rate,
        "frame_size": frame_size,
        "status": status,
    }
    if status != "error":
        entry["achieved_bps"] = result.get("achieved_bps")
        entry["loss_rate"] = result.get("loss_rate")
    benchmark_results.append(entry)
    if status == "error":
        pytest.skip(f"BLE queue full at rate={rate} frame_size={frame_size}")


def test_stability_echo_linux_to_mac(linux_target, benchmark_results, with_mac_peer):
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_echo(
        MAC_NPUB, count=SOAK_COUNT, payload_size=SOAK_PAYLOAD_SIZE,
    )
    assert result.get("status") != "error", f"Echo stability failed: {result}"
    loss_count = result.get("loss_count", 0)
    count = result.get("count", SOAK_COUNT)
    loss_rate = loss_count / max(count, 1)
    median_us = result.get("median_us", 0)
    p95_us = result.get("p95_us", 0)
    assert loss_rate <= SOAK_MAX_LOSS_RATE, (
        f"Stability echo loss too high: {loss_count}/{count} "
        f"({loss_rate:.1%}, max {SOAK_MAX_LOSS_RATE:.0%})"
    )
    benchmark_results.append({
        "test": "stability_echo",
        "pair": "linux->mac",
        "count": count,
        "payload_size": SOAK_PAYLOAD_SIZE,
        "loss_count": loss_count,
        "loss_rate": loss_rate,
        "median_us": median_us,
        "p95_us": p95_us,
    })


def test_stability_echo_mac_to_linux(mac_target, benchmark_results, with_linux_peer):
    fipsctl = mac_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_echo(
        LINUX_NPUB, count=SOAK_COUNT, payload_size=SOAK_PAYLOAD_SIZE,
    )
    assert result.get("status") != "error", f"Echo stability failed: {result}"
    loss_count = result.get("loss_count", 0)
    count = result.get("count", SOAK_COUNT)
    loss_rate = loss_count / max(count, 1)
    median_us = result.get("median_us", 0)
    p95_us = result.get("p95_us", 0)
    assert loss_rate <= SOAK_MAX_LOSS_RATE, (
        f"Stability echo loss too high: {loss_count}/{count} "
        f"({loss_rate:.1%}, max {SOAK_MAX_LOSS_RATE:.0%})"
    )
    benchmark_results.append({
        "test": "stability_echo",
        "pair": "mac->linux",
        "count": count,
        "payload_size": SOAK_PAYLOAD_SIZE,
        "loss_count": loss_count,
        "loss_rate": loss_rate,
        "median_us": median_us,
        "p95_us": p95_us,
    })


def test_reconnect_linux_to_mac(linux_target, with_mac_peer):
    fipsctl = linux_target.get_driver("FipsctlDriver")
    service = linux_target.get_driver("FipsServiceDriver")

    assert fipsctl.has_peer(MAC_NPUB), "Mac peer must be connected before reconnect test"

    service.restart()
    deadline = time.monotonic() + RECONNECT_TIMEOUT
    reconnected = False
    while time.monotonic() < deadline:
        try:
            if fipsctl.has_peer(MAC_NPUB):
                reconnected = True
                break
        except Exception:
            pass
        time.sleep(5)

    assert reconnected, (
        f"Mac peer did not reconnect within {RECONNECT_TIMEOUT}s after restart"
    )
