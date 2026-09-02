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
        binary = bench.build_firmware(
            bench.MICROFIPS_REPO,
            npub_hex=bench.lab_npub(bench.MICROFIPS_REPO, XX_DAEMON_MUL),
            nsec_hex="00" * 31 + "09",  # s3-lab board identity (G*9)
            features="noise-xx",
        )

        # 2. Daemon first: the node's pinned mDNS discovery needs the advert
        #    up before boot (same ordering as the IK scenarios).
        daemon.start()

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

        verdict = {
            "scenario": "bench_xx",
            "daemon_mul": XX_DAEMON_MUL,
            "pinned_discovery": console.count("mDNS: pinned FIPS peer discovered at"),
            "fmp_negotiation_agreed": console.count("fmp negotiation: agreed version"),
            "handshake_ok_count": console.count("handshake ok"),
            "silent_peer_rejections": console.count("policy: rejected"),
            "heartbeats_recv": console.count("steady: heartbeat received from peer"),
            "sender_reports_sent": console.count("sending sender report"),
            "daemon_peers": len(peers),
            "daemon_peer_addrs": [p.get("node_addr") for p in peers],
            "daemon_security_violations": daemon_log.count("SecurityViolation"),
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
        # Presence signature of the 2736f55 fix: the timer-branch reports
        # were previously sent but invisible (and uncounted).
        assert verdict["sender_reports_sent"] >= 10, verdict
        # Daemon side: exactly our node, connected, no violations.
        assert verdict["daemon_peers"] == 1, verdict
        assert S3_LAB_NODE_ADDR in verdict["daemon_peer_addrs"], verdict
        assert verdict["daemon_security_violations"] == 0, verdict
    finally:
        if tap:
            tap.stop()
        daemon.stop()
        lock.release()
