"""Chaos: malformed-frame storm vs a live session — unknown-unknowns probe.

Both protocol endpoints eat adversarial UDP while a real s3-lab session
runs: the fips daemon's input path AND our firmware's input path
(microfips #77 DoS-hardening class). The link under test must not notice:
heartbeats keep flowing, no teardown, both processes alive. Frame classes
are rate-limited (correctness probe, not bandwidth DoS — see
fips_lab.chaos).

A crash, teardown, or stall found here graduates into its own named
scenario with a minimal reproducer; this file stays the broad net.

Bisect knob (fips#154): CHAOS_STORMS=both (default, canonical net) |
node | daemon — one storm per run discriminates node-side RX loss
(rcvbuf crowding out real frames) from daemon-side misclassification
(junk tripping rekey/epoch-restart paths). Assertions are identical in
every mode; the default behavior is unchanged.

Run:
    pytest tests/test_chaos_fuzz.py -v                        # both storms
    CHAOS_STORMS=node pytest tests/test_chaos_fuzz.py -v      # bisect: node only
    CHAOS_STORMS=daemon pytest tests/test_chaos_fuzz.py -v    # bisect: daemon only
"""

import json
import os
import re
import time

import pytest

from fips_lab import bench
from fips_lab.chaos import FrameStorm

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"
S3_LAB_NSEC = "00" * 31 + "09"
LAB_DAEMON_NPUB = (
    "022f01e5e15cca351daff3843fb70f3c2f0a1bdd05e5af888a67784ef3e10a2a01"
)
LAB_DAEMON_UDP = ("192.168.13.221", 21213)

STORM_SECS = 90

STORM_TARGETS = os.environ.get("CHAOS_STORMS", "both").lower()
assert STORM_TARGETS in ("both", "node", "daemon"), (
    f"CHAOS_STORMS must be both|node|daemon, got {STORM_TARGETS!r}"
)


def _phase_counts(daemon_log: str, console: str) -> dict:
    """Session-health counters for one window (pre-storm vs whole run).

    The 2026-09-04 finding run showed the teardown cascade STARTING
    BEFORE the first storm frame reached either endpoint — so the
    pre-storm window is a first-class phase, not a footnote."""
    return {
        "promotions": daemon_log.count("Connection promoted"),
        "peer_restarts": daemon_log.count("Peer restart detected"),
        "resent_msg2": daemon_log.count("Resent msg2"),
        "rekey_responses": daemon_log.count("Sent rekey msg2 response"),
        "socket_installs": daemon_log.count("connected UDP socket installed"),
        "disconnects": daemon_log.count("Peer sent disconnect notification"),
        "heartbeats": console.count("heartbeat received from peer"),
        "node_rekey_answers": console.count("rekey msg1 received, answering"),
        "handshakes": console.count("handshake ok"),
    }


# daemon log: "... connected UDP socket installed peer=npub.. \
# peer_addr=192.168.13.N:PORT" — the node's send endpoint.
PEER_ADDR_RE = re.compile(r"connected UDP socket installed peer=\S+ "
                          r"peer_addr=(\d+\.\d+\.\d+\.\d+):(\d+)")

STORM_SECS = 90


def _node_udp_endpoint(daemon_log: str) -> tuple[str, int]:
    matches = PEER_ADDR_RE.findall(daemon_log)
    assert matches, "no connected UDP peer in daemon log — session not up?"
    host, port = matches[-1]
    return host, int(port)


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.timeout(900)
def test_chaos_fuzz_storm():
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)

    run_dir = bench.make_run_dir("chaos-fuzz")
    lock = bench.acquire_board_lock()
    tap = None
    daemon = None
    storms: list[FrameStorm] = []
    try:
        binary = bench.build_firmware(
            bench.MICROFIPS_REPO,
            npub_hex=LAB_DAEMON_NPUB,
            nsec_hex=S3_LAB_NSEC,
        )
        daemon = bench.LabDaemon(bench.MICROFIPS_REPO, 3600, run_dir)
        daemon.start()

        bench.quiesce_peer_radios(bench.MICROFIPS_REPO, S3_LAB_SERIAL)

        port = bench.find_board(serial=S3_LAB_SERIAL)
        bench.flash(port, binary)
        tap = bench.ConsoleTap(port, run_dir / "console.log")

        tap.wait_for("handshake ok", timeout=180)
        tap.wait_for("heartbeat received from peer", count=2, timeout=90)

        node_endpoint = _node_udp_endpoint(daemon.log_text())
        pre_daemon_log = daemon.log_text()
        (run_dir / "pre_storm_daemon.log").write_text(pre_daemon_log)
        pre = _phase_counts(pre_daemon_log, tap.read())

        daemon_storm = (
            FrameStorm(*LAB_DAEMON_UDP, seed=11).start()
            if STORM_TARGETS in ("both", "daemon") else None
        )
        node_storm = (
            FrameStorm(*node_endpoint, seed=23).start()
            if STORM_TARGETS in ("both", "node") else None
        )
        storms = [s for s in (daemon_storm, node_storm) if s is not None]
        assert storms, "CHAOS_STORMS selected no storms"

        hb_before = tap.read().count("heartbeat received from peer")
        time.sleep(STORM_SECS)
        hb_after = tap.read().count("heartbeat received from peer")

        # Post-storm settle: the link must still be live and exchanging.
        tap.wait_for("heartbeat received from peer", count=hb_after + 1,
                     timeout=60)
        time.sleep(10)

        console = tap.read()
        daemon_log = daemon.log_text()
        post = _phase_counts(daemon_log, console)
        verdict = {
            "scenario": "chaos_fuzz",
            "storm_secs": STORM_SECS,
            "storm_targets": STORM_TARGETS,
            "node_endpoint": f"{node_endpoint[0]}:{node_endpoint[1]}",
            "daemon_storm": daemon_storm.stats.as_dict() if daemon_storm else None,
            "node_storm": node_storm.stats.as_dict() if node_storm else None,
            "heartbeats_during_storm": hb_after - hb_before,
            "handshakes": console.count("handshake ok"),
            "node_reboots": console.count("ets Jun"),
            "daemon_promotions": daemon_log.count("Connection promoted"),
            "daemon_disconnects": daemon_log.count(
                "Peer sent disconnect notification"),
            "daemon_alive": daemon._proc is not None
                            and daemon._proc.poll() is None,
            "pre_storm": pre,
            "during_storm": {
                k: post[k] - pre[k] for k in post
            },
        }
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        assert verdict["heartbeats_during_storm"] >= 5, verdict
        assert verdict["handshakes"] == 1, verdict
        assert verdict["node_reboots"] == 0, verdict
        assert verdict["daemon_disconnects"] == 0, verdict
        assert verdict["daemon_alive"], verdict
    finally:
        for storm in storms:
            storm.stop()
        if tap:
            tap.stop()
        if daemon:
            daemon.stop()
        lock.release()
