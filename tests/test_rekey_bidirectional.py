"""Bidirectional rekey: the node self-rekeys (REKEY_AFTER_SECS=20) while the
daemon also rekeys — both directions rotate in the same session (the
"long-run interaction unobserved" known unknown from the 2026-09-01 rekey
arc, queue item 3).

Timer semantics (why the cadences must overlap): BOTH sides reset their
rekey timer on every rotation — the node resets session_started on its own
cutover AND on following a peer cutover, and the daemon's per-session
after_secs restarts whenever the node's rotation replaces the session.
The daemon draws jitter uniformly per session at construction (fips
node/mod.rs REKEY_JITTER_SECS=15), so its effective period V = after_secs
+ J is redrawn every rotation. Hardware-verified starvation bounds
(2026-09-02): daemon=120 — V is never under the node's ~33s cycle (daemon
starved, 6 node rotations); daemon=20 — most V draws sit under the node's
30s self-init dampening (node suppressed for the whole window, 3 daemon
rotations). The working point is daemon=32: the constraints "daemon fires
iff V < ~33s (node cycle)" and "node escapes iff V >= 30s (dampening)"
bracket V in a ~3s band, and D=32 centers the jitter draw on that band —
each direction advances on roughly every other rotation, so both occur
well within the wait budgets (per-run failure < ~1%).

Assertions: >=1 node-initiated rotation AND >=1 daemon-initiated rotation,
>=2 cutovers/drains total, exactly ONE session (zero rebuilds across the
interleaved rekeys), zero daemon disconnects / SecurityViolations, node
alive at the end.

Knob note: REKEY_AFTER_SECS compiles to a numeric constant (no greppable
binary string), so the knob landing is verified behaviorally — the
self-initiation lines below cannot occur with a stale 0 build.

Run:
    pytest tests/test_rekey_bidirectional.py -v
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
@pytest.mark.timeout(700)
def test_rekey_bidirectional(request):
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)

    run_dir = bench.make_run_dir("rekey-bidirectional")
    lock = bench.acquire_board_lock()
    tap = None
    daemon = None
    try:
        binary = bench.build_firmware(
            bench.MICROFIPS_REPO,
            npub_hex=LAB_DAEMON_NPUB,
            nsec_hex="00" * 31 + "09",
            extra_env={"REKEY_AFTER_SECS": "20"},
        )

        daemon = bench.LabDaemon(bench.MICROFIPS_REPO, 32, run_dir)
        daemon.start()

        port = bench.find_board(serial=S3_LAB_SERIAL)
        bench.flash(port, binary)
        tap = bench.ConsoleTap(port, run_dir / "console.log")

        tap.wait_for("handshake ok", timeout=90)
        # Node's own cycle ~33s; daemon's window 30-60s — either side may
        # fire first, and every rotation resets the other's timer, hence the
        # multi-round budgets below.
        tap.wait_for("rekey initiated, msg1 sent", timeout=90)
        tap.wait_for("rekey msg1 received", timeout=240)
        # Drain is peer-progress-aware (10s after last old-epoch frame).
        tap.wait_for("drain complete", count=2, timeout=90)
        time.sleep(2)

        console = tap.read()
        daemon_log = daemon.log_text()
        verdict = {
            "scenario": "rekey_bidirectional",
            "rekey_initiated": console.count("rekey initiated, msg1 sent"),
            "rekey_msg1_received": console.count("rekey msg1 received"),
            "cutover_complete": console.count("cutover complete"),
            "drain_complete": console.count("drain complete"),
            "handshake_ok_count": console.count("handshake ok"),
            "daemon_disconnect_notifications": daemon_log.count(
                "disconnect notification"
            ),
            "daemon_security_violations": daemon_log.count("SecurityViolation"),
            "node_alive": "steady: recv returned" in console[-2000:],
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        assert verdict["rekey_initiated"] >= 1, verdict
        assert verdict["rekey_msg1_received"] >= 1, verdict
        assert verdict["cutover_complete"] >= 2, verdict
        assert verdict["drain_complete"] >= 2, verdict
        assert verdict["handshake_ok_count"] == 1, verdict
        assert verdict["daemon_disconnect_notifications"] == 0, verdict
        assert verdict["daemon_security_violations"] == 0, verdict
        assert verdict["node_alive"], verdict
    finally:
        if tap:
            tap.stop()
        if daemon:
            daemon.stop()
        lock.release()
