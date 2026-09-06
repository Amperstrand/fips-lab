"""Hybrid transport path switching: ESP-NOW fallback and probe-gated return.

Closes fips-lab #6's hybrid half. The hybrid node keeps one pinned daemon
npub across both paths (WiFi UDP direct, ESP-NOW via gateway) so switching
changes the route, never the trust model. This scenario drives both
transitions deterministically on the one-S3 bench the #10 session proved
out: s3-lab = hybrid node (S3-only binary), atom-b = the D0WD
espnow-wifi-gw (first hardware run of the D0WD-class gateway — the S3
twin is the reference), lab daemon G*8.

Phase 1 — forced fallback + probe-gated return: the build pins
HYBRID_TEST_WIFI_DOWN_SECS=90 (boot skips WiFi, starts on ESP-NOW) and
HYBRID_WIFI_PROBE_SECS=120 (first probe window lands after the knob
expires, so one clean transition). Observables: node console
`boot WiFi skipped (test knob)` → ESP-NOW handshake through the gateway
(gateway `node locked`, daemon promotion) → `probe window` →
`switched to WiFi path` → re-handshake with the daemon reached directly
(new ARP entry on the workstation — ground truth that traffic left the
relay path, per the 'verify which path actually carries traffic' lesson).

Phase 2 — daemon loss on the WiFi path (the #6 RX-silence leg): daemon
SIGKILL (no goodbye) → node `link dead` on RX silence → daemon restart
(same identity) → re-handshake, heartbeats resume.

Run:
    pytest tests/test_hybrid_switch.py -v
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from fips_lab import bench

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"  # hybrid node
ATOM_B_SERIAL = "9D529068B4"  # D0WD esp-now WiFi gateway (atom-a browns out under association TX)
DAEMON_MUL = 8

WIFI_DOWN_SECS = 90
PROBE_SECS = 120
# Link-death budget: 30s RX-silence timeout + reconnect overhead.
LINK_DEAD_BUDGET_S = 90


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.timeout(1500)
def test_hybrid_switch(request):
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)
    if bench.find_board(vidpid=bench.D0WD_VIDPID, serial=ATOM_B_SERIAL) is None:
        pytest.skip(f"bench board {ATOM_B_SERIAL} not attached")

    run_dir = bench.make_run_dir(f"hybrid-switch-{request.node.name}")
    lock = bench.acquire_board_lock()
    taps: list[bench.ConsoleTap] = []
    daemon = None
    try:
        daemon = bench.LabDaemon(bench.MICROFIPS_REPO, 3600, run_dir, generator_mul=DAEMON_MUL)
        daemon.start()
        bench.quiesce_peer_radios(bench.MICROFIPS_REPO, ATOM_B_SERIAL)

        daemon_npub = bench.lab_npub(bench.MICROFIPS_REPO, DAEMON_MUL)

        gw_binary = bench.build_d0wd_espnow_wifi_gw(bench.MICROFIPS_REPO, daemon_npub)
        gw_port = bench.find_board(vidpid=bench.D0WD_VIDPID, serial=ATOM_B_SERIAL)
        bench.flash(gw_port, gw_binary, chip="esp32")
        tap_gw = bench.ConsoleTap(gw_port, run_dir / "console-gw.log", baud=115200)
        taps.append(tap_gw)
        tap_gw.wait_for("gateway: relaying ESP-NOW", timeout=150)

        node_binary = bench.build_s3_hybrid(
            bench.MICROFIPS_REPO, daemon_npub, bench.lab_nsec(bench.MICROFIPS_REPO, 9),
            wifi_down_secs=WIFI_DOWN_SECS, probe_secs=PROBE_SECS,
        )
        node_port = bench.find_board(serial=S3_LAB_SERIAL)
        bench.flash(node_port, node_binary)
        tap = bench.ConsoleTap(node_port, run_dir / "console-node.log")
        taps.append(tap)

        # --- Phase 1: knob-forced ESP-NOW start, probe-gated WiFi return --
        tap.wait_for("hybrid: boot WiFi skipped (test knob)", timeout=60)
        tap.wait_for("hybrid: starting on ESP-NOW path", timeout=30)
        # Full 13-channel sweep ≈ 80-120s per pass; bench radios vary —
        # run5 needed >2 passes to land (21 hops, no contact in 180s).
        tap.wait_for("handshake ok", timeout=330)
        tap.wait_for("heartbeat received", timeout=60)
        # The wifi-gw's log vocabulary has no per-node lock line (that's
        # the USB-bridged twin) — ESP-NOW-path evidence is the daemon
        # promoting the node while the node console shows no WiFi at all.
        espnow_promotions = daemon.log_text().count(
            "Connection promoted to active peer"
        )
        assert espnow_promotions >= 1
        espnow_heartbeats = tap.read().count("heartbeat received")

        tap.wait_for("hybrid: probe window", timeout=PROBE_SECS + 90)
        tap.wait_for("hybrid: switched to WiFi path", timeout=90)
        tap.wait_for("handshake ok", count=2, timeout=90)
        tap.wait_for("heartbeat received", count=espnow_heartbeats + 1, timeout=60)

        # Ground truth for the path switch (tcpdump, not ARP diffs — the
        # node's DHCP lease is stable across runs, so the workstation's
        # ARP table already holds it and a before/after diff sees nothing):
        # UDP to the daemon port from an address that is NOT the gateway
        # can only be the node talking direct.
        gw_ip = _gateway_ip(tap_gw.read())
        direct_capture = subprocess.run(
            ["sudo", "-n", "timeout", "10", "tcpdump", "-ni", "any", "-c", "3",
             f"udp port {bench.LAB_DAEMON_PORT} and not host {gw_ip}"],
            capture_output=True, text=True,
        )
        direct_lines = [
            line for line in direct_capture.stdout.splitlines()
            if " UDP " in line or "udp" in line.lower()
        ]

        # --- Phase 2: daemon loss on the WiFi path (RX silence) ----------
        daemon.stop(restore=False, graceful=False)
        t_kill = time.monotonic()
        tap.wait_for("link dead", timeout=LINK_DEAD_BUDGET_S)
        link_dead_s = time.monotonic() - t_kill
        daemon.start()
        tap.wait_for("handshake ok", count=3, timeout=120)
        tap.wait_for("heartbeat received", count=espnow_heartbeats + 3, timeout=90)

        console = tap.read()
        console_gw = tap_gw.read()
        verdict = {
            "scenario": "hybrid_switch",
            "knobs": {
                "HYBRID_TEST_WIFI_DOWN_SECS": WIFI_DOWN_SECS,
                "HYBRID_WIFI_PROBE_SECS": PROBE_SECS,
            },
            "link_dead_s": round(link_dead_s, 1),
            "espnow_heartbeats": espnow_heartbeats,
            "switched_to_wifi": console.count("hybrid: switched to WiFi path"),
            "switched_to_espnow": console.count("hybrid: switched to ESP-NOW path"),
            "handshake_ok_count": console.count("handshake ok"),
            "gateway_relaying": console_gw.count("gateway: relaying ESP-NOW"),
            "espnow_phase_promotions": espnow_promotions,
            "direct_packets": direct_lines[:3],
            "tcpdump_rc": direct_capture.returncode,
            "gateway_ip": gw_ip,
            "daemon_security_violations": daemon.log_text().count("SecurityViolation"),
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        assert verdict["switched_to_wifi"] == 1, verdict
        assert verdict["switched_to_espnow"] == 0, verdict
        # Path transitions legitimately re-handshake at session boundaries
        # (a post-switch mDNS miss keeps the endpoint and re-handshakes) —
        # require the full arc, not an exact count.
        assert verdict["handshake_ok_count"] >= 3, verdict
        assert verdict["espnow_phase_promotions"] >= 1, verdict
        assert verdict["gateway_relaying"] >= 1, verdict
        assert len(direct_lines) >= 1, (
            f"no direct (non-gateway) traffic to the daemon after the "
            f"switch — rc={direct_capture.returncode}, "
            f"stderr={direct_capture.stderr[-120:]!r}"
        )
        assert 10 <= verdict["link_dead_s"] <= LINK_DEAD_BUDGET_S, verdict
        assert verdict["daemon_security_violations"] == 0, verdict
    finally:
        for t in taps:
            t.stop()
        if daemon:
            daemon.stop()
        lock.release()


def _gateway_ip(console_gw: str) -> str | None:
    for line in console_gw.splitlines():
        if "gateway: WiFi connected, IP:" in line:
            return line.rsplit("IP:", 1)[1].split("/")[0].strip()
    return None
