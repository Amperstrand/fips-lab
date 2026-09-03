"""CDC bring-up: the STM32's USB-CDC surface as its own regression scenario
(audit #188 candidate 6 — scoped 2026-09-03: the M1/M2-era echo mode no
longer exists; the real surface is enumerate → DTR-gated MSG1 → bridge
handshake → heartbeats → soft-reset recovery).

What the mesh scenario (test_mcu_to_mcu_fsp) already covers and this
isolates to the STM32 alone:
- MSG1 latency after the bridge opens the port (wait_connection resolves
  on DTR; AGENTS.md claims ~0.5s — asserted < 15s here, the meaningful
  failure being "MCU sits in wait_connection forever").
- The soft-reset recovery cycle: `st-flash --connect-under-reset reset`
  mid-session → USB re-enumerates (possibly a different ttyACM number) →
  the single-hop bridge re-scans by VID:PID and reconnects (its
  `SERIAL reconnected` line) → the node reboots into wait_connection →
  re-handshake → heartbeats resume. This graduates the interactive
  "soft-reset re-enumeration intact" claim (#113 close-out) and the
  bridge auto-reconnect procedure.

The AGENTS.md "never st-flash reset during a live test" warning was about
the legacy 3-hop pipeline (stale-fd cascade). Here the bridge's ENODEV
reconnect IS the system under test — the reset is the point.

Probe provenance (playbook pattern 10): bridge TX/RX lines log
`>> CDC->UDP: frame#N <size>B` / `<< UDP->CDC: frame#N <size>B`; sizes
114B = IK MSG1, 69B = MSG2, 37B = established heartbeat frame. The bridge
frame counter is cumulative across serial reconnects, so post-reset
re-handshake traffic increments the same counters.

Run:
    pytest tests/test_cdc_bringup.py -v
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from fips_lab import bench

LAB_DAEMON_NPUB = (
    "022f01e5e15cca351daff3843fb70f3c2f0a1bdd05e5af888a67784ef3e10a2a01"
)
LAB_HOST = "192.168.13.221"
LAB_PORT = 21213
MSG1 = "114B"
MSG2 = "69B"
HB = "37B"


def wait_for_count(source, needle: str, count: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    got = source().count(needle)
    while time.monotonic() < deadline and got < count:
        time.sleep(1.0)
        got = source().count(needle)
    if got < count:
        raise TimeoutError(
            f"{needle!r} x{count} not reached in {timeout}s (got {got})"
        )
    return got


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.timeout(600)
def test_cdc_bringup():
    # Guard on the FLASH path (ST-Link), not the CDC runtime path: a
    # long-running MCU can wedge its USB stack (observed 2026-09-03: CDC
    # gone after hours of daemon-less retries, ST-Link alive) — the
    # flash's connect-under-reset reboot is the recovery, and a CDC that
    # STAYS dead after it is a real failure the scenario must catch.
    stlink = any(
        (Path("/sys/class/tty") / p.name / "device/../uevent")
        .read_text()
        .find("PRODUCT=483/374b")
        >= 0
        for p in Path("/dev").glob("ttyACM*")
    )
    if not stlink:
        pytest.skip("STM32 ST-Link not attached (no flash path)")
    if not bench.FIPS_BIN.exists():
        pytest.skip(f"fips binary missing at {bench.FIPS_BIN}")

    stm_bin = bench.build_stm32(bench.MICROFIPS_REPO, npub_hex=LAB_DAEMON_NPUB)

    run_dir = bench.make_run_dir("cdc-bringup")
    lock = bench.acquire_board_lock()
    bridge = None
    daemon = None
    try:
        # 3600 = rekey effectively off (0 is rejected: must exceed the
        # per-session ±15s jitter).
        daemon = bench.LabDaemon(bench.MICROFIPS_REPO, 3600, run_dir / "daemon")
        daemon.start()

        bench.flash_stm32(stm_bin)
        stm_port = None
        for _ in range(30):
            stm_port = bench.find_stm32_cdc()
            if stm_port:
                break
            time.sleep(1)
        assert stm_port is not None, "STM32 CDC (c0de:cafe) did not enumerate"

        bridge = bench.SerialBridge(
            bench.MICROFIPS_REPO, stm_port, 31337, LAB_HOST, LAB_PORT,
            run_dir / "bridge.log",
        )

        # DTR-gated bring-up: the port open asserts DTR, wait_connection()
        # resolves, MSG1 follows within ~0.5s of that. Bridge startup adds
        # python boot + port open; anything under 15s proves the gate.
        t_msg1 = bridge.wait_for(rf"frame#1 {MSG1}", timeout=90)
        assert t_msg1 <= 15, f"MSG1 took {t_msg1:.0f}s after bridge start"
        bridge.wait_for(rf"frame#[0-9]* {MSG2}", timeout=45)
        bridge.wait_for(rf"<< UDP->CDC: frame#[0-9]* {HB}", timeout=45)

        # Sustained heartbeats on a settled link (10s cadence).
        pre_hb = wait_for_count(lambda: bridge.read(), HB, 3, 60)

        # --- Soft-reset recovery cycle (the graduated procedure) ---
        pre_msg1 = bridge.read().count(MSG1)
        subprocess.run(
            ["st-flash", "--connect-under-reset", "reset"],
            check=True, capture_output=True, timeout=60,
        )

        # Bridge: detect the dead CDC fd, re-scan by VID:PID, reconnect
        # (possibly a different ttyACM number — that's the point).
        bridge.wait_for(r"SERIAL reconnected", timeout=120)

        # Node: reboots into wait_connection, the reconnect's open asserts
        # DTR, a fresh MSG1 goes out and the handshake re-runs.
        wait_for_count(lambda: bridge.read(), MSG1, pre_msg1 + 1, 120)
        post_hb = wait_for_count(lambda: bridge.read(), HB, pre_hb + 3, 180)

        stm_log = bridge.read()
        daemon_log = daemon.log_text()
        verdict = {
            "scenario": "cdc_bringup",
            "msg1_frames": stm_log.count(MSG1),
            "msg2_frames": stm_log.count(MSG2),
            "heartbeat_frames": stm_log.count(HB),
            "serial_reconnects": stm_log.count("SERIAL reconnected"),
            "recovery_heartbeats": post_hb - pre_hb,
            "daemon_promotions": daemon_log.count("Connection promoted"),
            "daemon_security_violations": daemon_log.count("SecurityViolation"),
            "daemon_disconnects": daemon_log.count("disconnect notification"),
            "t_msg1_secs": round(t_msg1, 1),
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        assert verdict["msg1_frames"] >= 2, verdict
        assert verdict["msg2_frames"] >= 2, verdict
        assert verdict["serial_reconnects"] >= 1, verdict
        assert verdict["recovery_heartbeats"] >= 3, verdict
        assert verdict["daemon_promotions"] >= 2, verdict
        assert verdict["daemon_security_violations"] == 0, verdict
        # Soft-recorded: a hard node reset may cost the daemon a link-dead
        # teardown line; promotions + resumed heartbeats are the contract.
    finally:
        if bridge:
            bridge.stop()
        if daemon:
            daemon.stop()
        lock.release()
