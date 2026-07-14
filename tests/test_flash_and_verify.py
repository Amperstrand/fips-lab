"""Full CI flash-and-verify flow: build, flash, reset, handshake, heartbeats.

Uses the same esp32/fips fixtures as test_esp32_l2cap.py — fully DRY.
Works with labgrid (--lg-env) or standalone SSH.

Usage:
    pytest tests/test_flash_and_verify.py -v -m flash
    pytest --lg-env=environment.yaml tests/test_flash_and_verify.py -v
"""

import time

import pytest

HANDSHAKE_TIMEOUT = 60


@pytest.fixture(scope="module", autouse=True)
def fips_running(fips):
    fips.restart()
    time.sleep(3)
    yield


@pytest.mark.flash
@pytest.mark.ci
@pytest.mark.hardware
class TestFlashAndVerify:

    def test_flash_and_handshake(self, esp32, fips, firmware_builder):
        """Full pipeline: build firmware, flash, reset, verify handshake."""
        elf_path = firmware_builder.build(features="l2cap")
        esp32.flash(elf_path)

        fips.restart()
        time.sleep(5)

        output = esp32.reset_and_capture(HANDSHAKE_TIMEOUT)
        assert "handshake ok" in output, (
            f"Handshake failed after flash. Output:\n{output[-500:]}"
        )

    def test_heartbeats_after_flash(self, esp32, fips, firmware_builder):
        """Flash fresh firmware, verify heartbeats flow."""
        elf_path = firmware_builder.build(features="l2cap")
        esp32.flash(elf_path)

        fips.restart()
        time.sleep(5)

        esp32.reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(90)

        stats = esp32.show_stats()
        assert stats.get("hb_tx", 0) > 0, "No heartbeats sent after flash"
        assert stats.get("hb_rx", 0) > 0, "No heartbeats received"
        assert stats.get("msg2_rx", 0) >= 1, "No successful handshakes"
