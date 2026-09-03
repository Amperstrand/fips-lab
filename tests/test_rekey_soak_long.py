"""Long-run rekey interleave soak: the bidirectional scenario's hour-scale
sibling (known unknown 1 from the 2026-09-02 handoff — the interleave was
only ever observed over ~2-5 min windows).

Same working point as test_rekey_bidirectional (node REKEY_AFTER_SECS=20
→ ~33s effective cycle with dampening; daemon after_secs=32 ± 15s jitter
→ V ∈ [17,47]s, centered on the ~3s overlap band). The soak asks whether
the interleave HOLDS at scale: ~50 rotation opportunities per 30 min
window, each rotation resetting the other side's timer — failure modes
only long runs expose (epoch divergence, drift into a starvation mode,
resource wedges).

Assert floors scale with the window (a fraction of the theoretical
rotation count), so the same file smoke-runs at REKEY_SOAK_SECS=120 and
soaks at the default 1800s. The first full run's observed counts are the
drift bound for future cron/nightly guarding.

Run (smoke, ~2 min window):
    REKEY_SOAK_SECS=120 pytest tests/test_rekey_soak_long.py -v
Run (full soak, ~30 min window):
    pytest tests/test_rekey_soak_long.py -v
"""

import json
import os
import time

import pytest

from fips_lab import bench

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"
LAB_DAEMON_NPUB = (
    "022f01e5e15cca351daff3843fb70f3c2f0a1bdd05e5af888a67784ef3e10a2a01"
)
NODE_REKEY_AFTER_SECS = 20
DAEMON_REKEY_AFTER_SECS = 32
NODE_CYCLE_SECS = 33  # dampening-inclusive upper bound (2026-09-02 model)
SOAK_WINDOW_SECS = int(os.environ.get("REKEY_SOAK_SECS", "1800"))
EXPECTED_ROTATIONS = max(1, SOAK_WINDOW_SECS // NODE_CYCLE_SECS)

ROTATION_FLOOR = max(1, EXPECTED_ROTATIONS // 10)
# The daemon's first rotation is a tail event at short windows: its V ∈
# [17,47]s is redrawn on every node rotation with only a ~35-43% fire
# chance per draw (the bidirectional scenario budgets 240s for it). Only
# assert daemon rotations once the window holds enough draws (~5+) that
# zero is vanishingly unlikely — the 120s smoke cannot demand one.
DAEMON_ROTATION_FLOOR = ROTATION_FLOOR if SOAK_WINDOW_SECS >= 360 else 0
CUTOVER_FLOOR = max(2, EXPECTED_ROTATIONS // 5)


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.slow
@pytest.mark.timeout(SOAK_WINDOW_SECS + 900)
def test_rekey_soak_long(request):
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)

    run_dir = bench.make_run_dir("rekey-soak-long")
    lock = bench.acquire_board_lock()
    tap = None
    daemon = None
    try:
        binary = bench.build_firmware(
            bench.MICROFIPS_REPO,
            npub_hex=LAB_DAEMON_NPUB,
            nsec_hex="00" * 31 + "09",
            extra_env={"REKEY_AFTER_SECS": str(NODE_REKEY_AFTER_SECS)},
        )

        daemon = bench.LabDaemon(
            bench.MICROFIPS_REPO, DAEMON_REKEY_AFTER_SECS, run_dir,
        )
        daemon.start()

        # Quiet bench: any other attached radio board (atoms on
        # L2CAP, CYD on old WiFi firmware) peers with scenario
        # daemons and perturbs the session under test (2026-09-03,
        # see bench.quiesce_peer_radios).
        bench.quiesce_peer_radios(bench.MICROFIPS_REPO, S3_LAB_SERIAL)

        port = bench.find_board(serial=S3_LAB_SERIAL)
        bench.flash(port, binary)
        tap = bench.ConsoleTap(port, run_dir / "console.log")

        tap.wait_for("handshake ok", timeout=90)

        # Batched settle (playbook: one wait, one evidence sweep) — every
        # rotation's evidence accumulates in the console + daemon logs.
        time.sleep(SOAK_WINDOW_SECS)

        console = tap.read()
        daemon_log = daemon.log_text()
        verdict = {
            "scenario": "rekey_soak_long",
            "window_secs": SOAK_WINDOW_SECS,
            "expected_rotations": EXPECTED_ROTATIONS,
            "rekey_initiated": console.count("rekey initiated, msg1 sent"),
            "rekey_msg1_received": console.count("rekey msg1 received"),
            "cutover_complete": console.count("cutover complete"),
            "drain_complete": console.count("drain complete"),
            "heartbeats_rx": console.count("heartbeat received"),
            "handshake_ok_count": console.count("handshake ok"),
            "daemon_disconnect_notifications": daemon_log.count(
                "disconnect notification"
            ),
            "daemon_security_violations": daemon_log.count("SecurityViolation"),
            "node_alive": "steady: recv returned" in console[-2000:],
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        assert verdict["rekey_initiated"] >= ROTATION_FLOOR, verdict
        assert verdict["rekey_msg1_received"] >= DAEMON_ROTATION_FLOOR, verdict
        assert verdict["cutover_complete"] >= CUTOVER_FLOOR, verdict
        assert verdict["drain_complete"] >= CUTOVER_FLOOR, verdict
        assert verdict["heartbeats_rx"] >= SOAK_WINDOW_SECS // 30, verdict
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
