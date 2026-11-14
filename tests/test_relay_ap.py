"""Relay AP with onboard peer: one S3 proves the relay-ap bring-up path.

First bench scenario for the relay-ap firmware — the relay-ap half of the
microfips #253 merge gate (esp-radio 1.0.0-beta.1 migration; the relay-ap
port compiled green but has had zero radio time). Topology = the
2026-08-22 hardware-verified Router+peer configuration on a single board:
the S3 runs `microfips-esp32s3-relay-ap-peer` — an open AP (bench-specific
SSID via RELAY_AP_SSID so the relay never collides with a real !FIPS AP)
whose uplink joins the lab WiFi and mDNS-discovers the lab daemon, plus a
full FIPS node (the compiled-in s3-lab identity) that peers with the
daemon over the relay's uplink. Verdict = the 2026-08-22 observables:
uplink associated, pinned upstream found, peer handshake + entering
steady, sustained heartbeats, zero SecurityViolations.

Scope note: the single-board variant exercises uplink association, AP
bring-up, mDNS-pinned upstream discovery, and the peer session — the
surfaces the esp-radio API migration rewrote. The client-behind-AP chain
(DHCP leases + UDP relay hop) needs a second S3 as client and stays with
the two-board variant/follow-up.

Run:
    pytest tests/test_relay_ap.py -v
"""

import json

import pytest

from fips_lab import bench

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"  # relay AP + peer node (s3-lab identity)
DAEMON_MUL = 8
RELAY_AP_SSID = "!FIPS-BENCH"

# Uplink assoc + DHCP + mDNS discovery + FSP session setup, with bench
# WiFi variance; the 2026-08-22 interactive pass settled in ~10s.
BOOT_BUDGET_S = 180
HEARTBEATS_MIN = 2


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.slow
@pytest.mark.timeout(900)
def test_relay_ap_peer(request):
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)

    run_dir = bench.make_run_dir(f"relay-ap-{request.node.name}")
    lock = bench.acquire_board_lock()
    taps: list[bench.ConsoleTap] = []
    daemon = None
    try:
        daemon = bench.LabDaemon(bench.MICROFIPS_REPO, 3600, run_dir, generator_mul=DAEMON_MUL)
        daemon.start()
        bench.quiesce_peer_radios(bench.MICROFIPS_REPO, S3_LAB_SERIAL)

        daemon_npub = bench.lab_npub(bench.MICROFIPS_REPO, DAEMON_MUL)
        node_nsec = bench.lab_nsec(bench.MICROFIPS_REPO, 9)

        binary = bench.build_s3_relay_ap_peer(
            bench.MICROFIPS_REPO, daemon_npub, node_nsec, RELAY_AP_SSID,
        )
        node_port = bench.find_board(serial=S3_LAB_SERIAL)
        bench.flash(node_port, binary)
        tap = bench.ConsoleTap(node_port, run_dir / "console-relay-peer.log")
        taps.append(tap)

        # Upstream: uplink association -> mDNS-pinned daemon endpoint.
        tap.wait_for("relay: uplink connected, IP:", timeout=BOOT_BUDGET_S)
        tap.wait_for("relay: upstream FIPS endpoint", timeout=60)
        # Peer node: Noise IK over the relay uplink, entering steady.
        tap.wait_for("session: handshake ok, entering steady", timeout=60)
        # Sustained: two heartbeat intervals past steady — a one-shot
        # handshake alone would not prove the session survives.
        tap.wait_for("steady: heartbeat received from peer",
                     count=HEARTBEATS_MIN, timeout=90)

        console = tap.read()
        verdict = {
            "scenario": "relay_ap_peer",
            "relay_ap_ssid": RELAY_AP_SSID,
            "uplink_connected": console.count("relay: uplink connected, IP:"),
            "upstream_found": console.count("relay: upstream FIPS endpoint"),
            "handshake_ok": console.count("session: handshake ok, entering steady"),
            "heartbeats_received": console.count("steady: heartbeat received from peer"),
            # 0 expected on the single-board variant: no external client
            # joins the AP, so the DHCP/relay-hop surface is unexercised
            # (two-board variant covers it). Counted, not asserted.
            "dhcp_leases": console.count("relay: DHCP reply to"),
            "daemon_promotions": daemon.log_text().count("Connection promoted to active peer"),
            "daemon_security_violations": daemon.log_text().count("SecurityViolation"),
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        assert verdict["uplink_connected"] >= 1, verdict
        assert verdict["upstream_found"] >= 1, verdict
        assert verdict["handshake_ok"] >= 1, verdict
        assert verdict["heartbeats_received"] >= HEARTBEATS_MIN, verdict
        assert verdict["daemon_promotions"] >= 1, verdict
        assert verdict["daemon_security_violations"] == 0, verdict
    finally:
        for t in taps:
            t.stop()
        if daemon:
            daemon.stop()
        lock.release()
