"""Rekey soak: the daemon rekeys, the node answers and follows the cutover.

Codifies the 2026-09-01 interactive session (microfips #183, fips-lab #5):
flash pinned firmware, join the lab AP, handshake against the isolated lab
daemon with a scenario rekey cadence, then survive >= 2 full rekey cycles
with zero session rebuilds and zero daemon-side disconnects.

Requires the microfips bench (s3-lab board + lab AP + .env + esp toolchain);
skips with a reason otherwise. The daemon enforces after_secs > 15s (per-session
rekey jitter is ±15s), so the fast cadence is 20s; a full run including build
and flash is ~3-4 min.

Run:
    pytest tests/test_rekey_soak.py -k fast -v
"""

import json
import time

import pytest

from fips_lab import bench

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"
@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.parametrize(
    "rekey_after_secs",
    [
        20,
        pytest.param(
            120,
            marks=[pytest.mark.slow, pytest.mark.timeout(900)],
        ),
    ],
    ids=["fast", "stock"],
)
def test_rekey_soak(rekey_after_secs, request):
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)

    run_dir = bench.make_run_dir(f"rekeysoak-{request.node.callspec.id}")
    ids = bench.BenchIdentities(bench.MICROFIPS_REPO)
    lock = bench.acquire_board_lock()
    tap = None
    daemon = None
    try:
        # 1. Verified build (raises on stale pins before touching hardware).
        binary = bench.build_firmware(
            bench.MICROFIPS_REPO,
            npub_hex=ids.npub("daemon"),
            nsec_hex=ids.nsec("s3-lab"),
            extra_env=bench.lab_static_target_env(),
        )

        # 2. Daemon first: the node's pinned mDNS discovery needs the advert
        #    up before boot, or it falls back to the compiled-in VPS target.
        daemon = bench.LabDaemon(
            bench.MICROFIPS_REPO, rekey_after_secs, run_dir,
            nsec_hex=ids.nsec("daemon"),
        )
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

        # 4. Wait for the boot session, then soak two full rekey cycles.
        tap.wait_for("handshake ok", timeout=90)
        soak_timeout = max(90.0, 3.0 * rekey_after_secs + 60.0)
        t0 = time.monotonic()
        tap.wait_for("cutover complete", count=2, timeout=soak_timeout)
        soak_elapsed = time.monotonic() - t0
        # Drain is peer-progress-aware (10s after last old-epoch frame).
        tap.wait_for("drain complete", count=2, timeout=30)
        time.sleep(2)

        console = tap.read()
        daemon_log = daemon.log_text()

        verdict = {
            "scenario": "rekey_soak",
            "rekey_after_secs": rekey_after_secs,
            "rekey_msg1_received": console.count("rekey msg1 received"),
            "cutover_complete": console.count("cutover complete"),
            "drain_complete": console.count("drain complete"),
            "handshake_ok_count": console.count("handshake ok"),
            "daemon_disconnect_notifications": daemon_log.count(
                "disconnect notification"
            ),
            "soak_elapsed_s": round(soak_elapsed, 1),
            "node_alive": "steady: recv returned" in console[-2000:],
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))
        ids.save(run_dir)

        assert verdict["cutover_complete"] >= 2, verdict
        assert verdict["rekey_msg1_received"] >= 2, verdict
        assert verdict["drain_complete"] >= 2, verdict
        # Exactly one session (the boot one): zero rebuilds across the rekeys.
        assert verdict["handshake_ok_count"] == 1, verdict
        # The absence signature: the pre-rekey firmware died here every cycle.
        assert verdict["daemon_disconnect_notifications"] == 0, verdict
        assert verdict["node_alive"], verdict
    finally:
        if tap:
            tap.stop()
        if daemon:
            daemon.stop()
        lock.release()
