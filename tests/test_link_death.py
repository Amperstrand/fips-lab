"""Link death: RX-silence timeout, teardown, and auto-reconnect.

Codifies the ESP-NOW lesson ('sends keep succeeding at MAC level while the
daemon is unreachable — RX silence is the only death signal') as a hardware
regression (audit #188 candidate 1, microfips #189).

Flow: steady session → daemon stops (node untouched) → node's link-dead
timeout fires (console `steady: link dead`) → daemon restarts with the same
derived identity → node reconnects (2nd `handshake ok`) → heartbeats resume.
The teardown must be a clean silence-timeout, not a bad-frame storm: the
daemon-side log must show zero SecurityViolation disconnects.

Run:
    pytest tests/test_link_death.py -v
"""

import json
import time

import pytest

from fips_lab import bench

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"
LAB_DAEMON_NPUB = (
    "022f01e5e15cca351daff3843fb70f3c2f0a1bdd05e5af888a67784ef3e10a2a01"
)


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.timeout(420)
def test_link_death_and_reconnect(request):
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)

    node_nsec = "00" * 31 + "09"
    run_dir = bench.make_run_dir(f"linkdeath-{request.node.name}")
    lock = bench.acquire_board_lock()
    tap = None
    daemon = None
    try:
        binary = bench.build_firmware(
            bench.MICROFIPS_REPO,
            npub_hex=LAB_DAEMON_NPUB,
            nsec_hex=node_nsec,
        )
        daemon = bench.LabDaemon(bench.MICROFIPS_REPO, 3600, run_dir)
        daemon.start()

        port = bench.find_board(serial=S3_LAB_SERIAL)
        bench.flash(port, binary)
        tap = bench.ConsoleTap(port, run_dir / "console.log")

        # Steady state confirmed by heartbeats, not just the handshake.
        tap.wait_for("handshake ok", timeout=90)
        tap.wait_for("heartbeat received", timeout=60)

        # Kill the daemon WITHOUT a goodbye (SIGKILL): a graceful stop sends
        # disconnect notifications, which gives the node a clean PeerDC
        # instead of the RX-silence path this scenario exists to prove.
        # The node's sends still succeed (UDP), so silence is the only signal.
        daemon.stop(restore=False, graceful=False)
        t_kill = time.monotonic()
        tap.wait_for("link dead", timeout=75)
        silence_s = time.monotonic() - t_kill

        # Same derived identity (lab_keygen G*8) → same pinned peer.
        daemon.start()
        tap.wait_for("handshake ok", count=2, timeout=120)
        tap.wait_for("heartbeat received", count=2, timeout=60)

        console = tap.read()
        daemon_log = daemon.log_text()
        verdict = {
            "scenario": "link_death",
            "silence_to_link_dead_s": round(silence_s, 1),
            "handshake_ok_count": console.count("handshake ok"),
            "heartbeats_after_reconnect": console.count("heartbeat received"),
            "daemon_security_violations": daemon_log.count("SecurityViolation"),
            "daemon_promotions": daemon_log.count(
                "Connection promoted to active peer"
            ),
            "node_alive": "steady: recv returned" in console[-2000:],
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        # Silence-timeout fired in a plausible window (not instant, not hung).
        assert 10 <= verdict["silence_to_link_dead_s"] <= 75, verdict
        assert verdict["handshake_ok_count"] == 2, verdict
        assert verdict["heartbeats_after_reconnect"] >= 2, verdict
        # The daemon log restarts with the daemon; its fresh session must be
        # clean — SecurityViolation here would mean a bad-frame storm instead
        # of a clean silence-timeout teardown.
        assert verdict["daemon_security_violations"] == 0, verdict
        assert verdict["daemon_promotions"] >= 1, verdict
        assert verdict["node_alive"], verdict
    finally:
        if tap:
            tap.stop()
        if daemon:
            daemon.stop()
        lock.release()
