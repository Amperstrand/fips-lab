"""Self-initiated rekey: the NODE rotates keys on its own cadence and the
daemon follows (#183 Phase-4 hardware verification — the follow-up noted
in its close comment).

Build with REKEY_AFTER_SECS=20 (binary-verified via the knob's log
string); the daemon rekeys slowly (3600s) so any rotation in the window
is the node's. Assertions: >= 2 self-initiated cycles (cutover lines),
drain-with-zeroization per cycle, exactly ONE session (zero rebuilds),
node alive at the end, daemon side shows zero SecurityViolation
disconnects (a failed rotation would storm bad frames instead).

Run:
    pytest tests/test_rekey_self_initiated.py -v
"""

import json

import pytest

from fips_lab import bench

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"
LAB_DAEMON_NPUB = (
    "022f01e5e15cca351daff3843fb70f3c2f0a1bdd05e5af888a67784ef3e10a2a01"
)
@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.timeout(420)
def test_rekey_self_initiated(request):
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)

    run_dir = bench.make_run_dir("rekey-self-init")
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

        # Daemon rekeys at 3600s: rotations in the window are the NODE's.
        daemon = bench.LabDaemon(bench.MICROFIPS_REPO, 3600, run_dir)
        daemon.start()

        port = bench.find_board(serial=S3_LAB_SERIAL)
        bench.flash(port, binary)
        tap = bench.ConsoleTap(port, run_dir / "console.log")

        tap.wait_for("handshake ok", timeout=90)
        # Two full self-initiated cycles at ~20s each (± per-session jitter
        # does not apply — that's the daemon's responder-side knob).
        tap.wait_for("cutover complete", count=2, timeout=150)
        tap.wait_for("drain complete", count=2, timeout=30)
        time_settle = 2
        import time as _t
        _t.sleep(time_settle)

        console = tap.read()
        daemon_log = daemon.log_text()
        verdict = {
            "scenario": "rekey_self_initiated",
            "rekey_initiated": console.count("rekey initiated, msg1 sent"),
            "cutover_complete": console.count("cutover complete"),
            "drain_complete": console.count("drain complete"),
            "handshake_ok_count": console.count("handshake ok"),
            "daemon_security_violations": daemon_log.count("SecurityViolation"),
            "node_alive": "steady: recv returned" in console[-2000:],
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        assert verdict["rekey_initiated"] >= 2, verdict
        assert verdict["cutover_complete"] >= 2, verdict
        assert verdict["drain_complete"] >= 2, verdict
        assert verdict["handshake_ok_count"] == 1, verdict
        assert verdict["daemon_security_violations"] == 0, verdict
        assert verdict["node_alive"], verdict
    finally:
        if tap:
            tap.stop()
        if daemon:
            daemon.stop()
        lock.release()
