"""Bench XX: FMP-v1 + Noise-XX wire against an upstream-next daemon on hardware.

Codifies the 2026-09-02 #193 session: an ESP32-S3 (s3-lab, G·9) running
`--features noise-xx` firmware handshakes with a next-branch daemon
(0.6.0-dev, G·22, port 21214) discovered via pinned mDNS, negotiates FMP
version 1, and holds ONE session with sustained bidirectional heartbeats.

That session also found the silent-peer churn bug (fixed in microfips
2736f55): the policy's data-frame counter was only fed through FrameAction
dispatch, so against a daemon that exchanges no FSP the node tore down a
healthy link every ~link_dead_timeout. The `handshake_ok == 1` and
`policy: rejected == 0` asserts below are the regression guard for it.

#196 (2026-09-03) tightened the MMP behavior to upstream semantics: report
sends are gated on the negotiated provides/wants bits (a Leaf sends
ReceiverReports only, data-gated to the heartbeat cadence) and the XX wire
uses next's slim [format_version][total_length] report layout — so the
daemon parses every report and actually measures RTT. The
`sender_reports == 0`, `receiver_reports >= 3`, `daemon_malformed == 0`,
and `daemon_rtt_measured` asserts guard that.

Requires: the microfips bench (s3-lab board + lab AP + .env + esp
toolchain) AND a prebuilt next-branch daemon worktree (FIPS_NEXT_BIN —
see microfips AGENTS.md "#193 recipe"; /tmp worktrees are disposable, so
this scenario skips with the rebuild pointer when it is gone). Daemon
identity G·22: registry boards use 1-12, the mdns rogue 20, interop sim
21 — no key is ever shared with an IK deployment.

Run:
    pytest tests/test_bench_xx.py -v
"""

import json
import re
import time

import pytest

from fips_lab import bench

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"
S3_LAB_NODE_ADDR = "1c6ad0339ceb13433701dc6b4349363a"  # G*9 (registry)
XX_DAEMON_MUL = 22
STEADY_WINDOW_SECS = 90  # >= 2x the pre-fix ~33s churn + hb margin


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.timeout(600)
def test_bench_xx():
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)

    run_dir = bench.make_run_dir("bench-xx")
    daemon = bench.BenchXxDaemon(bench.MICROFIPS_REPO, run_dir, generator_mul=XX_DAEMON_MUL)
    xx_skip = daemon.available()
    if xx_skip:
        pytest.skip(xx_skip)

    lock = bench.acquire_board_lock()
    tap = None
    try:
        # 1. Verified XX build: pins the G*22 daemon npub, carries the
        #    noise-xx negotiation marker (binary-checked in build_firmware).
        #    #192 upgrade: the FSP dual-mode initiator targets the daemon
        #    (FIPS_FSP_TARGET_* knobs, microfips) instead of the STM32 mesh
        #    default — the session layer's first hardware proof (the link
        #    layer was #193; the session layer was sim-only until now).
        xx_npub = bench.lab_npub(bench.MICROFIPS_REPO, XX_DAEMON_MUL)
        xx_addr = bench.lab_node_addr(bench.MICROFIPS_REPO, XX_DAEMON_MUL)
        binary = bench.build_firmware(
            bench.MICROFIPS_REPO,
            npub_hex=xx_npub,
            nsec_hex="00" * 31 + "09",  # s3-lab board identity (G*9)
            features="noise-xx",
            extra_env={
                "FIPS_FSP_TARGET_NPUB_HEX": xx_npub,
                "FIPS_FSP_TARGET_NODE_ADDR_HEX": xx_addr,
            }
            # Static fallback = the XX daemon's own port (21214): pinned mDNS
            # stays the asserted primary (the "discovered at" line only
            # prints on a real hit), but a discovery miss no longer falls
            # back to the dead VPS and eats the handshake budget (09-05).
            | bench.lab_static_target_env(port=21214),
        )
        # The FSP target addr knob compiles to bytes (16B) — binary-check
        # it like the pins above (stale-knob guard, playbook pattern #4).
        assert bytes.fromhex(xx_addr) in binary.read_bytes(), \
            "FSP target node addr missing from binary (stale knob?)"

        # 2. Daemon first: the node's pinned mDNS discovery needs the advert
        #    up before boot (same ordering as the IK scenarios).
        daemon.start()

        # Quiet bench: any other attached radio board (atoms on
        # L2CAP, CYD on old WiFi firmware) peers with scenario
        # daemons and perturbs the session under test (2026-09-03,
        # see bench.quiesce_peer_radios).
        bench.quiesce_peer_radios(bench.MICROFIPS_REPO, S3_LAB_SERIAL)

        # 3. Flash + tap.
        port = bench.find_board(serial=S3_LAB_SERIAL)
        bench.flash(port, binary)
        tap = bench.ConsoleTap(port, run_dir / "console.log")

        # 4. Boot session, then hold the steady window.
        tap.wait_for("handshake ok", timeout=90)
        tap.wait_for("fmp negotiation: agreed version", timeout=10)
        time.sleep(STEADY_WINDOW_SECS)

        console = tap.read()
        peers = daemon.peers()
        daemon_log = daemon.log_text()
        # tracing writes ANSI color codes around structured fields, so
        # `rtt=` never appears literally in the raw log — strip them
        # before pattern-matching metric values.
        daemon_log_plain = re.sub(r"\x1b\[[0-9;]*m", "", daemon_log)

        verdict = {
            "scenario": "bench_xx",
            "daemon_mul": XX_DAEMON_MUL,
            "pinned_discovery": console.count("mDNS: pinned FIPS peer discovered at"),
            "fmp_negotiation_agreed": console.count("fmp negotiation: agreed version"),
            "handshake_ok_count": console.count("handshake ok"),
            "silent_peer_rejections": console.count("policy: rejected"),
            "heartbeats_recv": console.count("steady: heartbeat received from peer"),
            # #196: against a Full daemon a Leaf sends ReceiverReports only
            # (negotiated provides/wants gate) — SenderReports are OFF.
            "sender_reports_sent": console.count("sending sender report"),
            "receiver_reports_sent": console.count("sending receiver report"),
            "daemon_peers": len(peers),
            "daemon_peer_addrs": [p.get("node_addr") for p in peers],
            "daemon_security_violations": daemon_log.count("SecurityViolation"),
            "daemon_malformed_reports": daemon_log.count("Malformed"),
            "daemon_rtt_measured": bool(re.search(r"rtt=\d", daemon_log_plain)),
            # #192 hardware upgrade: FSP session layer over the XX wire —
            # the node (dual-mode initiator, targeted at the daemon via the
            # FIPS_FSP_TARGET_* knobs) must drive setup -> ack -> msg3 and
            # the daemon must land the responder session.
            "fsp_setup_sent": console.count("sending session datagram"),
            "fsp_ack_in": console.count("fsp_type=0x02"),  # SessionAck inbound
            "daemon_session_setup": daemon_log.count("SessionSetup processed (XX)"),
            "daemon_session_established": daemon_log.count(
                "Session established (responder, XX)"
            ),
            "steady_window_s": STEADY_WINDOW_SECS,
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        assert verdict["pinned_discovery"] >= 1, verdict
        assert verdict["fmp_negotiation_agreed"] >= 1, verdict
        # Exactly one session across the window — the pre-fix firmware
        # re-handshook every ~33s right here.
        assert verdict["handshake_ok_count"] == 1, verdict
        assert verdict["silent_peer_rejections"] == 0, verdict
        assert verdict["heartbeats_recv"] >= 3, verdict
        # Presence signature of the 2736f55 fix (report sends feed the
        # policy): the ReceiverReport timer path is the live send channel
        # since #196 gated SenderReports off for leaves.
        assert verdict["receiver_reports_sent"] >= 3, verdict
        assert verdict["sender_reports_sent"] == 0, verdict
        # Daemon side: exactly our node, connected, no violations, reports
        # parsed (no Malformed lines) and RTT actually measured from them.
        assert verdict["daemon_peers"] == 1, verdict
        assert S3_LAB_NODE_ADDR in verdict["daemon_peer_addrs"], verdict
        assert verdict["daemon_security_violations"] == 0, verdict
        assert verdict["daemon_malformed_reports"] == 0, verdict
        assert verdict["daemon_rtt_measured"], verdict
        # #192 hardware upgrade: the XX session layer end-to-end.
        assert verdict["fsp_setup_sent"] >= 2, verdict  # SessionSetup + msg3
        assert verdict["fsp_ack_in"] >= 1, verdict  # daemon SessionAck (split-tolerant read)
        assert verdict["daemon_session_setup"] >= 1, verdict
        assert verdict["daemon_session_established"] >= 1, verdict
    finally:
        if tap:
            tap.stop()
        daemon.stop()
        lock.release()
