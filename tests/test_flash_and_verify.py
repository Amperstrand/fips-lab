"""Full CI flash-and-verify flow: build → flash → reset → handshake → heartbeats.

This is the most valuable test for regression catching — it verifies the
entire pipeline from firmware build to BLE L2CAP link establishment.

Prerequisites:
  - ai-legion-small has the microfips repo with ESP toolchain
  - ai-legion has the ESP32 connected via CP210x
  - FIPS daemon running on ai-legion-small

Usage:
    pytest tests/test_flash_and_verify.py -v -m flash
    pytest tests/test_flash_and_verify.py::test_flash_and_handshake -v
"""

import time

import pytest

BUILD_TIMEOUT = 600
HANDSHAKE_TIMEOUT = 60


@pytest.fixture(scope="module")
def built_firmware(fips):
    """Build the latest firmware on ai-legion-small, return remote path."""
    import subprocess

    result = subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "ubuntu@ai-legion-small",
            "cd /home/ubuntu/src/microfips && "
            "git fetch origin && git reset --hard origin/main && "
            "export PATH=/home/ubuntu/.rustup/toolchains/esp/bin:"
            "/home/ubuntu/.rustup/toolchains/esp/xtensa-esp-elf/"
            "esp-15.2.0_20250920/xtensa-esp-elf/bin:"
            "/home/ubuntu/.cargo/bin:$PATH && "
            "export LIBCLANG_PATH=/home/ubuntu/.rustup/toolchains/esp/"
            "xtensa-esp32-elf-clang/esp-20.1.1_20250829/esp-clang/lib && "
            "export RUSTUP_TOOLCHAIN=esp && "
            "cargo build -p microfips-esp32 --release "
            "--target xtensa-esp32-none-elf "
            "-Zbuild-std=core,alloc --features l2cap 2>&1 | tail -1 && "
            "echo BUILD_OK",
        ],
        capture_output=True, text=True, timeout=BUILD_TIMEOUT,
    )

    if "BUILD_OK" not in result.stdout:
        pytest.fail(f"Firmware build failed: {result.stdout[-500:]}")

    return (
        "/home/ubuntu/src/microfips/target/"
        "xtensa-esp32-none-elf/release/microfips-esp32-l2cap"
    )


@pytest.mark.flash
@pytest.mark.ci
@pytest.mark.hardware
class TestFlashAndVerify:

    def test_flash_and_handshake(self, esp32, fips, built_firmware):
        """Full pipeline: build → flash → reset → verify handshake."""
        import subprocess

        # Convert ELF to binary on ai-legion-small
        subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "ubuntu@ai-legion-small",
                f"export PATH=/home/ubuntu/.rustup/toolchains/esp/bin:$PATH && "
                f"esptool --chip esp32 elf2image {built_firmware} "
                f"--output /tmp/fips-ci-flash.bin && echo CONVERT_OK",
            ],
            capture_output=True, text=True, timeout=60,
        )

        # Copy binary to ai-legion
        import shutil
        scp_result = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "ubuntu@ai-legion-small",
                "cat /tmp/fips-ci-flash.bin",
            ],
            capture_output=True, timeout=30,
        )
        subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "ubuntu@ai-legion",
                "cat > /tmp/fips-ci-flash.bin",
            ],
            input=scp_result.stdout, capture_output=True, timeout=30,
        )

        # Flash
        subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "ubuntu@ai-legion",
                "sudo esptool --chip esp32 --port /dev/ttyUSB0 "
                "--before default-reset -b 460800 "
                "write-flash 0x10000 /tmp/fips-ci-flash.bin 2>&1 | tail -1",
            ],
            capture_output=True, text=True, timeout=120,
        )

        # Restart FIPS and verify handshake
        fips.restart()
        time.sleep(5)

        output = esp32.reset_and_capture(HANDSHAKE_TIMEOUT)
        assert "handshake ok" in output, (
            f"Handshake failed after flash. Serial output:\n{output[-500:]}"
        )

    def test_heartbeats_after_flash(self, esp32, fips):
        """Verify heartbeats flow after a fresh flash."""
        fips.restart()
        time.sleep(5)

        esp32.reset_and_capture(HANDSHAKE_TIMEOUT)
        time.sleep(90)

        stats = esp32.show_stats()
        assert stats.get("hb_tx", 0) > 0, "No heartbeats sent after flash"
        assert stats.get("hb_rx", 0) > 0, "No heartbeats received after flash"
        assert stats.get("msg2_rx", 0) >= 1, "No successful handshakes"
