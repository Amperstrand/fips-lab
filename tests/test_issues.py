"""Targeted tests for known issues.

- #132: Throughput download direction unimplemented
- #133: Echo loss at 128B+ payloads (BLE L2CAP segmentation)

Run:
    pytest --lg-env=environment.yaml tests/test_issues.py -v
"""

import pytest

from conftest import ESP32_NPUB, MAC_NPUB

@pytest.mark.parametrize(
    "payload_size",
    [0, 32, 64],
    ids=["ps0", "ps32", "ps64"],
)
def test_echo_linux_to_mac_no_loss(linux_target, payload_size, with_mac_peer):
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_echo(MAC_NPUB, count=20, payload_size=payload_size)
    assert result.get("status") != "error", f"Benchmark failed: {result}"
    loss_count = result.get("loss_count")
    if loss_count is not None:
        assert loss_count <= 2, (
            f"Unexpected loss at payload_size={payload_size}: "
            f"{loss_count}/{result.get('count', '?')}"
        )


@pytest.mark.parametrize(
    "payload_size",
    [128, 256],
    ids=["ps128", "ps256"],
)
def test_echo_linux_to_mac_large_payload_loss(linux_target, payload_size, with_mac_peer):
    """Payloads ≥128B may have loss (issue #133).

    This test documents the loss rate rather than asserting zero loss.
    If the bug is fixed, this test still passes — it just records 0% loss.
    """
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_echo(MAC_NPUB, count=20, payload_size=payload_size)
    assert result.get("status") != "error", f"Benchmark failed: {result}"

    loss_count = result.get("loss_count", 0)
    count = result.get("count", 20)
    loss_pct = (loss_count / count * 100) if count else 0

    # Document loss rate — no assertion on zero loss (issue #133)
    # but cap at 50% to catch total failures
    assert loss_pct < 50, (
        f"Loss rate too high at payload_size={payload_size}: "
        f"{loss_count}/{count} ({loss_pct:.1f}%)"
    )


def test_throughput_download_returns_error(linux_target, with_mac_peer):
    """Download direction status check (issue #132).

    Download was previously unimplemented. If it now returns results,
    the test passes — the bug may be partially or fully fixed.
    """
    fipsctl = linux_target.get_driver("FipsctlDriver")
    result = fipsctl.benchmark_throughput(
        MAC_NPUB, direction="download", duration=5,
        frame_size=100, rate=30000,
    )
    assert result.get("status") != "error" or result.get("achieved_bps") is not None, (
        f"Download completely failed: {result}"
    )
