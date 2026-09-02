"""MCU-to-MCU mesh: STM32 (CDC bridge) + ESP32-S3 (WiFi) both peer with the
lab daemon — the bench shape of scripts/test_mcu_to_mcu_fsp.sh without the
VPS dependency (audit #188 candidate 3).

Hard assertions: both nodes promoted by the daemon (2 distinct peers),
sustained heartbeats on BOTH bridges/nodes, zero disconnects.
Soft-recorded (verdict, no assert): FSP SessionSetup/ACK frames between the
MCUs — whether the S3 WiFi node auto-initiates FSP toward its compiled-in
STM32 target over the mesh is a known unknown this scenario observes.

Run:
    pytest tests/test_mcu_to_mcu_fsp.py -v
"""

import json
import time

import pytest

from fips_lab import bench

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"
LAB_DAEMON_NPUB = (
    "022f01e5e15cca351daff3843fb70f3c2f0a1bdd05e5af888a67784ef3e10a2a01"
)
LAB_HOST = "192.168.13.221"
LAB_PORT = 21213


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.timeout(700)
def test_mcu_to_mcu_mesh(request):
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)

    stm_bin = bench.build_stm32(bench.MICROFIPS_REPO, npub_hex=LAB_DAEMON_NPUB)
    s3_bin = bench.build_firmware(
        bench.MICROFIPS_REPO,
        npub_hex=LAB_DAEMON_NPUB,
        nsec_hex="00" * 31 + "09",
    )

    run_dir = bench.make_run_dir("mcu-to-mcu")
    lock = bench.acquire_board_lock()
    s3_tap = None
    daemon = None
    stm_bridge = None
    try:
        daemon = bench.LabDaemon(bench.MICROFIPS_REPO, 3600, run_dir / "daemon")
        daemon.start()

        # --- STM32: flash via st-flash, then CDC bridge to the daemon ---
        bench.flash_stm32(stm_bin)
        # st-flash reset churns the USB CDC port — poll for enumeration.
        stm_port = None
        for _ in range(30):
            stm_port = bench.find_stm32_cdc()
            if stm_port:
                break
            time.sleep(1)
        assert stm_port is not None, "STM32 CDC (c0de:cafe) did not enumerate"
        stm_bridge = bench.SerialBridge(
            bench.MICROFIPS_REPO, stm_port, 31337, LAB_HOST, LAB_PORT,
            run_dir / "bridge-stm32.log",
        )

        # --- S3: flash + tap (direct WiFi to the daemon) ---
        bench.flash(port := bench.find_board(serial=S3_LAB_SERIAL), s3_bin)
        s3_tap = bench.ConsoleTap(port, run_dir / "console-s3.log")

        # --- Both nodes into steady ---
        stm_bridge.wait_for(r"frame#1 114B", timeout=90)      # STM32 MSG1
        stm_bridge.wait_for(r"frame#[0-9]* 69B", timeout=90)  # STM32 MSG2
        s3_tap.wait_for("handshake ok", timeout=120)

        # Sustained mesh traffic: heartbeats both directions on the bridge.
        stm_bridge.wait_for(r"<< UDP->CDC: frame#[0-9]* 37B", timeout=120)

        console_s3 = s3_tap.read()
        daemon_log = daemon.log_text()
        stm_log = stm_bridge.read()

        verdict = {
            "scenario": "mcu_to_mcu_mesh",
            "stm32_promotions": daemon_log.count("Connection promoted"),
            "stm32_msg1": stm_log.count("114B"),
            "stm32_heartbeats": stm_log.count("37B"),
            "s3_handshake_ok": console_s3.count("handshake ok"),
            "s3_heartbeats": console_s3.count("heartbeat received"),
            # Soft observations (known unknown: does the S3 auto-initiate
            # FSP toward its compiled-in STM32 target over the mesh?)
            "fsp_session_setup_seen_on_stm32": stm_log.count("149B"),
            "s3_session_datagram_sends": console_s3.count("type=0x00"),
            "daemon_disconnects": daemon_log.count("disconnect notification"),
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        assert verdict["stm32_promotions"] >= 2, verdict
        assert verdict["stm32_msg1"] >= 1, verdict
        assert verdict["stm32_heartbeats"] >= 2, verdict
        assert verdict["s3_handshake_ok"] == 1, verdict
        assert verdict["s3_heartbeats"] >= 1, verdict
        assert verdict["daemon_disconnects"] == 0, verdict
    finally:
        if s3_tap:
            s3_tap.stop()
        if stm_bridge:
            stm_bridge.stop()
        if daemon:
            daemon.stop()
        lock.release()
