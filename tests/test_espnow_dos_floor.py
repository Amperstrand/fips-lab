"""ESP-NOW DoS floor: sticky peer slot + reconnect floor (microfips #77).

The #77 firmware hardening landed host-side (microfips 0a7cc88 reconnect
floor, 62954b5 sticky PeerSlot); this scenario is the bench close-out leg
(fips-lab #10). Runs on the one-S3 bench: the standalone WiFi gateway
needs the S3, and the ESP-NOW leaf firmware runs on the D0WD atoms —
incumbent on atom-a (G*11), attacker on atom-b (G*12), lab daemon G*8.

Phase A — reconnect floor: a peer that completes handshakes then tears
the session down inside the 5s base interval (daemon graceful stop = a
clean PeerDC, the fastest teardown the node can see) must not cycle the
node through full Noise IK faster than the floor. Observable: spacing
between consecutive `handshake ok` console lines >= 4s (floor is 5s from
the attempt stamp; polling jitter eats ~1s). Pre-0a7cc88 firmware clears
the stamp on handshake ok and cycles at ~1-2s.

Phase B — sticky peer slot: while the incumbent owns the gateway's MAC
slot (active heartbeats), a second ESP-NOW source channel-sweeping its
handshake broadcasts must be dropped before reassembly. Observables:
gateway console shows zero `node slot moved`; the incumbent session
survives (no new `handshake ok`, no `link dead`, heartbeats keep
arriving); the daemon never promotes a second peer; the attacker itself
never handshakes or locks. Pre-62954b5 firmware is last-speaker-wins:
the attacker's frames steal the relay and starve the incumbent into
`link dead`.

Set FIPS_LAB_FW_REPO to a microfips checkout/worktree to build the
firmware from another source state (the RED-proof pattern: 0a7cc88^ has
both bugs).

Run:
    pytest tests/test_espnow_dos_floor.py -v
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from fips_lab import bench

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"  # gateway (the only S3 on the bench)
ATOM_A_SERIAL = "81528A13B6"  # incumbent node, G*11
ATOM_B_SERIAL = "9D529068B4"  # foreign source, G*12
DAEMON_MUL = 8

FLOOR_CYCLES = 4
# Policy floor is 5s from the attempt stamp; handshake-ok line spacing
# carries ±1s of tap-poll jitter on top of it. Pre-fix cycles at ~1-2s.
MIN_HANDSHAKE_SPACING_S = 4.0
BURST_WINDOW_S = 120


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.timeout(1500)
def test_espnow_dos_floor(request):
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)
    for serial in (ATOM_A_SERIAL, ATOM_B_SERIAL):
        if bench.find_board(vidpid=bench.D0WD_VIDPID, serial=serial) is None:
            pytest.skip(f"bench board {serial} not attached")

    fw_repo = Path(os.environ.get("FIPS_LAB_FW_REPO", bench.MICROFIPS_REPO))
    daemon_npub = bench.lab_npub(fw_repo, DAEMON_MUL)
    run_dir = bench.make_run_dir(f"espnow-dos-floor-{request.node.name}")
    lock = bench.acquire_board_lock()
    taps: list[bench.ConsoleTap] = []
    daemon = None
    try:
        daemon = bench.LabDaemon(fw_repo, 3600, run_dir, generator_mul=DAEMON_MUL)
        daemon.start()

        # Quiet bench: atom-b stays radio-silent through Phase A; the CYD's
        # old WiFi firmware would trust-on-first-advert peer with the
        # scenario daemon (2026-09-03 interference).
        bench.quiesce_peer_radios(fw_repo, ATOM_A_SERIAL)

        gw_binary = bench.build_s3_espnow_wifi_gw(fw_repo, daemon_npub)
        gw_port = bench.find_board(serial=S3_LAB_SERIAL)
        bench.flash(gw_port, gw_binary)
        tap_gw = bench.ConsoleTap(gw_port, run_dir / "console-gw.log")
        taps.append(tap_gw)
        tap_gw.wait_for("gateway: relaying ESP-NOW", timeout=120)

        node_binary = bench.build_d0wd_espnow(
            fw_repo, daemon_npub, bench.lab_nsec(fw_repo, 11)
        )
        node_port = bench.find_board(vidpid=bench.D0WD_VIDPID, serial=ATOM_A_SERIAL)
        bench.flash(node_port, node_binary, chip="esp32")
        tap_a = bench.ConsoleTap(node_port, run_dir / "console-a.log", baud=115200)
        taps.append(tap_a)

        # Channel-sweep discovery can take a full 13-channel sweep (~80s).
        tap_a.wait_for("handshake ok", timeout=180)
        tap_a.wait_for("heartbeat received", timeout=60)

        # --- Phase A: malicious-fast teardown/reconnect cycles ----------
        arrivals = [time.monotonic()]
        for cycle in range(FLOOR_CYCLES):
            daemon.stop(restore=False, graceful=True)  # clean PeerDC
            daemon.start()  # back before the node's floor expires
            tap_a.wait_for("handshake ok", count=cycle + 2, timeout=90)
            arrivals.append(time.monotonic())
        spacings = [
            round(b - a, 2) for a, b in zip(arrivals, arrivals[1:])
        ]
        console_a = tap_a.read()

        phase_a = {
            "handshake_spacings_s": spacings,
            "min_spacing_s": min(spacings),
            "handshake_ok_count": console_a.count("handshake ok"),
            "backoff_lines": console_a.count("policy: reconnect backoff"),
            "link_dead": console_a.count("link dead"),
        }
        promotions_before = daemon.log_text().count(
            "Connection promoted to active peer"
        )
        heartbeats_before = tap_a.read().count("heartbeat received")

        foreign_binary = bench.build_d0wd_espnow(
            fw_repo, daemon_npub, bench.lab_nsec(fw_repo, 12)
        )
        foreign_port = bench.find_board(
            vidpid=bench.D0WD_VIDPID, serial=ATOM_B_SERIAL
        )
        bench.flash(foreign_port, foreign_binary, chip="esp32")
        tap_b = bench.ConsoleTap(
            foreign_port, run_dir / "console-b.log", baud=115200
        )
        taps.append(tap_b)

        # The attacker must actually be on the air: a full 13-channel
        # sweep guarantees its broadcasts crossed the gateway's channel.
        tap_b.wait_for("ESP-NOW mode starting", timeout=60)
        tap_b.wait_for(
            "discovery sweep, hopping to channel", count=13, timeout=240
        )

        time.sleep(BURST_WINDOW_S)
        console_a = tap_a.read()
        console_gw = tap_gw.read()
        console_b = tap_b.read()
        promotions_after = daemon.log_text().count(
            "Connection promoted to active peer"
        )

        phase_b = {
            "burst_window_s": BURST_WINDOW_S,
            "attacker_sweep_hops": console_b.count("discovery sweep"),
            "attacker_handshake_ok": console_b.count("handshake ok"),
            "attacker_peer_locked": console_b.count("peer locked"),
            "gateway_slot_moves": console_gw.count("node slot moved"),
            "incumbent_handshake_ok": console_a.count("handshake ok"),
            "incumbent_link_dead": console_a.count("link dead"),
            "incumbent_heartbeats_delta": console_a.count(
                "heartbeat received"
            ) - heartbeats_before,
            "daemon_promotions_delta": promotions_after - promotions_before,
            "daemon_security_violations": daemon.log_text().count(
                "SecurityViolation"
            ),
        }

        verdict = {
            "scenario": "espnow_dos_floor",
            "fw_repo": str(fw_repo),
            "fw_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=fw_repo, text=True
            ).strip()[:12],
            "floor": phase_a,
            "sticky_slot": phase_b,
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        # Phase A: the floor holds under a peer cycling teardowns. The
        # hard invariant is spacing (>= floor - jitter); the backoff LINE
        # only prints when the floor still has remainder at check time —
        # a loaded bench can stretch the daemon's graceful-stop notify
        # past the 5s floor, in which case the cycle is naturally floored
        # and no line prints (2026-09-05 rerun: 5 oks, 0 lines, spacings
        # all >5s). Binding evidence at least once keeps the guard's teeth.
        assert len(spacings) == FLOOR_CYCLES, verdict
        assert phase_a["min_spacing_s"] >= MIN_HANDSHAKE_SPACING_S, verdict
        assert phase_a["handshake_ok_count"] == FLOOR_CYCLES + 1, verdict
        assert phase_a["backoff_lines"] >= 1, verdict
        assert phase_a["link_dead"] == 0, verdict

        # Phase B: the foreign source never touches the slot or the daemon,
        # and the incumbent session rides out the burst untouched.
        assert phase_b["attacker_sweep_hops"] >= 13, verdict
        assert phase_b["attacker_handshake_ok"] == 0, verdict
        assert phase_b["attacker_peer_locked"] == 0, verdict
        assert phase_b["gateway_slot_moves"] == 0, verdict
        assert phase_b["incumbent_handshake_ok"] == FLOOR_CYCLES + 1, verdict
        assert phase_b["incumbent_link_dead"] == 0, verdict
        assert phase_b["incumbent_heartbeats_delta"] >= 6, verdict
        assert phase_b["daemon_promotions_delta"] == 0, verdict
        assert phase_b["daemon_security_violations"] == 0, verdict
    finally:
        for tap in taps:
            tap.stop()
        if daemon:
            daemon.stop()
        lock.release()
