"""FipsStrategy state-machine tests.

Verifies that FipsStrategy.transition() correctly drives the fips service
through its lifecycle: off → deployed → connected → ready.

Run:
    pytest --lg-env=environment.yaml tests/test_strategy.py -v
"""

import pytest

from fips_lab.strategy.fips import FipsStatus


@pytest.fixture
def linux_strategy(linux_target, request):
    linux_target.activate("FipsServiceDriver")
    linux_target.activate("FipsctlDriver")
    strategy = linux_target.get_driver("FipsStrategy")

    def _restore_service():
        service = linux_target.get_driver("FipsServiceDriver")
        service.start()

    request.addfinalizer(_restore_service)
    return strategy


def test_linux_strategy_deploy(linux_strategy, linux_target):
    """Transition linux-218 to 'deployed' (service started, waiting for peers)."""
    linux_strategy.transition(FipsStatus.deployed)
    assert linux_strategy.status == FipsStatus.deployed

    fipsctl = linux_target.get_driver("FipsctlDriver")
    status = fipsctl.show_status()
    assert status.get("state") == "running", f"Expected running, got: {status}"


@pytest.mark.xfail(reason="BLE peer reconnection takes >5s after service restart", strict=False)
def test_linux_strategy_connected(linux_strategy, linux_target):
    """Transition linux-218 to 'connected' (service running with at least one peer)."""
    linux_strategy.transition(FipsStatus.connected)
    assert linux_strategy.status == FipsStatus.connected

    fipsctl = linux_target.get_driver("FipsctlDriver")
    peers = fipsctl.show_peers()
    assert len(peers) > 0, "No peers connected after transition to 'connected'"


def test_linux_strategy_off(linux_strategy):
    """Transition linux-218 to 'off' (service stopped)."""
    linux_strategy.transition(FipsStatus.off)
    assert linux_strategy.status == FipsStatus.off


def test_linux_strategy_force(linux_target):
    """Force-set strategy state without side effects."""
    strategy = linux_target.get_driver("FipsStrategy")
    strategy.force(FipsStatus.ready)
    assert strategy.status == FipsStatus.ready
