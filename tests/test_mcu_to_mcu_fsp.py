"""MCU-to-MCU mesh: STM32 (CDC bridge) + ESP32-S3 (WiFi) both peer with the
lab daemon — the bench shape of scripts/test_mcu_to_mcu_fsp.sh without the
VPS dependency (audit #188 candidate 3).

Hard assertions: both nodes promoted by the daemon (2 distinct peers),
sustained heartbeats on BOTH bridges/nodes, zero disconnects, and the S3
auto-initiates FSP toward its compiled-in STM32 target — FULL session
content: SessionSetup -> ACK, msg3, then PING/PONG (promoted 2026-09-02
from sends/ACKs >=1 after observing green-run artifacts).

Probe provenance (playbook pattern 10 — sizes derived, not guessed):
- PING/PONG are 4-byte payloads: session datagram len=73 (35B body +
  AEAD(4B)), FMP frame 110B. SessionSetup is len=111 frame=148B, msg3 is
  len=115 frame=152B — all three distinguishable by size.
- Inbound discriminator: node.rs/fsp_handler.rs log
  `fsp: datagram in len=.. fsp_type=.. src={addr[0:2]}..{addr[14:16]}`;
  the STM32's registry NodeAddr is 132f39a9...f295 -> `src=132f..f295`.
  fsp_type: 0x02 = SessionAck, 0x00 = established-phase data (PONGs).
- The PONG itself: microfips-service FspServiceAdapter special-cases
  payload == b"PING" -> replies b"PONG"; the S3's test_ping detection is
  sim-only, so the PONG is visible only via the inbound log line.
- Initiator arms FSP_START_DELAY=5s after handshake, PINGs every 10s in
  Established; the 30s settle window covers several round-trips.

Run:
    pytest tests/test_mcu_to_mcu_fsp.py -v
"""

import json
import re
import time

import pytest

from fips_lab import bench

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"
LAB_DAEMON_NPUB = (
    "022f01e5e15cca351daff3843fb70f3c2f0a1bdd05e5af888a67784ef3e10a2a01"
)
LAB_HOST = "192.168.13.221"
LAB_PORT = 21213
STM32_SRC_PREFIX = "132f"
S3_PING_SEND = "sending session datagram type=0x00 len=73 frame=110B"
STM32_PONG_IN = f"fsp: datagram in len=73 fsp_type=0x00 src={STM32_SRC_PREFIX}"
STM32_ACK_IN = f"fsp: datagram in len=135 fsp_type=0x02 src={STM32_SRC_PREFIX}"


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

        # Quiet bench: any other attached radio board (atoms on
        # L2CAP, CYD on old WiFi firmware) peers with scenario
        # daemons and perturbs the session under test (2026-09-03,
        # see bench.quiesce_peer_radios).
        bench.quiesce_peer_radios(bench.MICROFIPS_REPO, S3_LAB_SERIAL)

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

        # Batched settle window (playbook: one wait, one evidence sweep):
        # setup+ack+msg3 take seconds, then PINGs every 10s in Established —
        # 30s covers several full PING/PONG round-trips.
        time.sleep(30)

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
            "s3_ping_sends": console_s3.count(S3_PING_SEND),
            "s3_pongs_from_stm32": console_s3.count(STM32_PONG_IN),
            "s3_session_ack_from_stm32": console_s3.count(STM32_ACK_IN),
            "s3_session_datagram_sends_total": console_s3.count(
                "sending session datagram"
            ),
            "s3_datagram_recvs_total": console_s3.count("fsp: datagram in"),
            "stm32_inbound_frame_sizes": sorted(
                re.findall(r"UDP->CDC: frame#\d+ (\d+)B", stm_log)
            ),
            "daemon_disconnects": daemon_log.count("disconnect notification"),
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        assert verdict["stm32_promotions"] >= 2, verdict
        assert verdict["stm32_msg1"] >= 1, verdict
        assert verdict["stm32_heartbeats"] >= 2, verdict
        assert verdict["s3_handshake_ok"] == 1, verdict
        assert verdict["s3_heartbeats"] >= 1, verdict
        assert verdict["s3_ping_sends"] >= 1, verdict
        assert verdict["s3_pongs_from_stm32"] >= 1, verdict
        assert verdict["s3_session_ack_from_stm32"] >= 1, verdict
        assert verdict["daemon_disconnects"] == 0, verdict
    finally:
        if s3_tap:
            s3_tap.stop()
        if stm_bridge:
            stm_bridge.stop()
        if daemon:
            daemon.stop()
        lock.release()
