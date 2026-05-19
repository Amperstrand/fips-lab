"""Parametrized BLE benchmark matrix tests.

Run:
    pytest --lg-env=environment.yaml tests/test_benchmark.py -v
    pytest -m benchmark ...
"""

import pytest

from conftest import ESP32_NPUB, LINUX_NPUB, MAC_NPUB

ECHO_PAYLOAD_SIZES = [0, 32, 64, 128, 256]
ECHO_COUNT = 20
ECHO_MAX_MEDIAN_MS = 500
ECHO_MAX_LOSS_SMALL_PAYLOAD = 2

THROUGHPUT_FRAME_SIZES = [20, 50, 100]
THROUGHPUT_DURATION = 5
THROUGHPUT_RATE = 30000


def _assert_echo(result: dict[str, object], payload_size: int) -> None:
    assert result.get("status") != "error", f"Echo benchmark failed: {result}"
    loss_count = result.get("loss_count")
    if loss_count is not None and payload_size <= 64:
        assert loss_count <= ECHO_MAX_LOSS_SMALL_PAYLOAD, (
            f"Echo loss at payload_size={payload_size}: "
            f"{loss_count}/{result.get('count', '?')} (max {ECHO_MAX_LOSS_SMALL_PAYLOAD})"
        )
    median_us = result.get("median_us")
    if isinstance(median_us, (int, float)):
        median_ms = median_us / 1000
        assert median_ms < ECHO_MAX_MEDIAN_MS, (
            f"Median RTT too high: {median_ms:.1f}ms"
        )


def _assert_throughput(result: dict[str, object]) -> None:
    assert result.get("status") != "error", f"Throughput benchmark failed: {result}"
    assert result.get("achieved_bps") is not None, f"Missing achieved_bps: {result}"


def _record(
    benchmark_results: list[dict[str, object]],
    test_type: str,
    pair: str,
    result: dict[str, object],
    **extra: object,
) -> None:
    entry = {"test": test_type, "pair": pair, **extra, **result}
    benchmark_results.append(entry)


@pytest.mark.benchmark
@pytest.mark.parametrize(
    "payload_size",
    ECHO_PAYLOAD_SIZES,
    ids=[f"ps{ps}" for ps in ECHO_PAYLOAD_SIZES],
)
def test_echo_linux_to_mac(linux_target, benchmark_results, payload_size, with_mac_peer):
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_echo(
        MAC_NPUB, count=ECHO_COUNT, payload_size=payload_size,
    )
    _assert_echo(result, payload_size)
    _record(benchmark_results, "echo", "linux->mac", result,
            payload_size=payload_size)


@pytest.mark.benchmark
@pytest.mark.xfail(reason="ESP32 BLE L2CAP cross-connection instability, issue #133", strict=False)
@pytest.mark.parametrize(
    "payload_size",
    ECHO_PAYLOAD_SIZES,
    ids=[f"ps{ps}" for ps in ECHO_PAYLOAD_SIZES],
)
def test_echo_linux_to_esp32(linux_target, benchmark_results, payload_size, with_esp32_peer):
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_echo(
        ESP32_NPUB, count=ECHO_COUNT, payload_size=payload_size,
    )
    _assert_echo(result, payload_size)
    _record(benchmark_results, "echo", "linux->esp32", result,
            payload_size=payload_size)


@pytest.mark.benchmark
@pytest.mark.parametrize(
    "payload_size",
    ECHO_PAYLOAD_SIZES,
    ids=[f"ps{ps}" for ps in ECHO_PAYLOAD_SIZES],
)
def test_echo_mac_to_linux(mac_target, benchmark_results, payload_size, with_linux_peer):
    fipsctl = mac_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_echo(
        LINUX_NPUB, count=ECHO_COUNT, payload_size=payload_size,
    )
    _assert_echo(result, payload_size)
    _record(benchmark_results, "echo", "mac->linux", result,
            payload_size=payload_size)


@pytest.mark.benchmark
@pytest.mark.skip(reason="ESP32 cannot initiate benchmarks")
@pytest.mark.parametrize(
    "payload_size",
    ECHO_PAYLOAD_SIZES,
    ids=[f"ps{ps}" for ps in ECHO_PAYLOAD_SIZES],
)
def test_echo_esp32_to_linux(esp32_target, benchmark_results, payload_size):
    fipsctl = esp32_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_echo(
        LINUX_NPUB, count=ECHO_COUNT, payload_size=payload_size,
    )
    _assert_echo(result, payload_size)
    _record(benchmark_results, "echo", "esp32->linux", result,
            payload_size=payload_size)


@pytest.mark.benchmark
@pytest.mark.parametrize(
    "frame_size",
    THROUGHPUT_FRAME_SIZES,
    ids=[f"fs{fs}" for fs in THROUGHPUT_FRAME_SIZES],
)
def test_throughput_upload_linux_to_mac(linux_target, benchmark_results, frame_size, with_mac_peer):
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_throughput(
        MAC_NPUB,
        direction="upload",
        duration=THROUGHPUT_DURATION,
        frame_size=frame_size,
        rate=THROUGHPUT_RATE,
    )
    _assert_throughput(result)
    _record(benchmark_results, "throughput", "linux->mac", result,
            direction="upload", frame_size=frame_size)


@pytest.mark.benchmark
@pytest.mark.xfail(reason="ESP32 BLE L2CAP cross-connection instability, issue #133", strict=False)
@pytest.mark.parametrize(
    "frame_size",
    THROUGHPUT_FRAME_SIZES,
    ids=[f"fs{fs}" for fs in THROUGHPUT_FRAME_SIZES],
)
def test_throughput_upload_linux_to_esp32(linux_target, benchmark_results, frame_size, with_esp32_peer):
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_throughput(
        ESP32_NPUB,
        direction="upload",
        duration=THROUGHPUT_DURATION,
        frame_size=frame_size,
        rate=THROUGHPUT_RATE,
    )
    _assert_throughput(result)
    _record(benchmark_results, "throughput", "linux->esp32", result,
            direction="upload", frame_size=frame_size)


@pytest.mark.benchmark
@pytest.mark.parametrize(
    "frame_size",
    THROUGHPUT_FRAME_SIZES,
    ids=[f"fs{fs}" for fs in THROUGHPUT_FRAME_SIZES],
)
def test_throughput_upload_mac_to_linux(mac_target, benchmark_results, frame_size, with_linux_peer):
    fipsctl = mac_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_throughput(
        LINUX_NPUB,
        direction="upload",
        duration=THROUGHPUT_DURATION,
        frame_size=frame_size,
        rate=THROUGHPUT_RATE,
    )
    _assert_throughput(result)
    _record(benchmark_results, "throughput", "mac->linux", result,
            direction="upload", frame_size=frame_size)


@pytest.mark.benchmark
@pytest.mark.skip(reason="ESP32 cannot initiate benchmarks")
@pytest.mark.parametrize(
    "frame_size",
    THROUGHPUT_FRAME_SIZES,
    ids=[f"fs{fs}" for fs in THROUGHPUT_FRAME_SIZES],
)
def test_throughput_upload_esp32_to_linux(esp32_target, benchmark_results, frame_size):
    fipsctl = esp32_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_throughput(
        LINUX_NPUB,
        direction="upload",
        duration=THROUGHPUT_DURATION,
        frame_size=frame_size,
        rate=THROUGHPUT_RATE,
    )
    _assert_throughput(result)
    _record(benchmark_results, "throughput", "esp32->linux", result,
            direction="upload", frame_size=frame_size)


@pytest.mark.benchmark
@pytest.mark.skip(reason="Download direction unimplemented, issue #132")
@pytest.mark.parametrize(
    "frame_size",
    THROUGHPUT_FRAME_SIZES,
    ids=[f"fs{fs}" for fs in THROUGHPUT_FRAME_SIZES],
)
def test_throughput_download_linux_to_mac(linux_target, benchmark_results, frame_size):
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_throughput(
        MAC_NPUB,
        direction="download",
        duration=THROUGHPUT_DURATION,
        frame_size=frame_size,
        rate=THROUGHPUT_RATE,
    )
    _assert_throughput(result)
    _record(benchmark_results, "throughput", "linux->mac", result,
            direction="download", frame_size=frame_size)


@pytest.mark.benchmark
@pytest.mark.skip(reason="Download direction unimplemented, issue #132")
@pytest.mark.parametrize(
    "frame_size",
    THROUGHPUT_FRAME_SIZES,
    ids=[f"fs{fs}" for fs in THROUGHPUT_FRAME_SIZES],
)
def test_throughput_download_linux_to_esp32(linux_target, benchmark_results, frame_size):
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_throughput(
        ESP32_NPUB,
        direction="download",
        duration=THROUGHPUT_DURATION,
        frame_size=frame_size,
        rate=THROUGHPUT_RATE,
    )
    _assert_throughput(result)
    _record(benchmark_results, "throughput", "linux->esp32", result,
            direction="download", frame_size=frame_size)
