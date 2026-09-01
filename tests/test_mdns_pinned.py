"""mDNS pinned discovery: the node pins to the compiled-in peer key and
never handshakes a rogue advert on the same LAN.

Codifies the pinned-accept / rogue-reject decision (audit #188 candidate 2;
the behavior was only ever proven interactively, 2026-08-18 Walter session).
Two daemons advertise `_fips._udp.local.` concurrently: the legit lab-daemon
(G*8, port 21213) and a rogue (G*20 — an unregistered multiplier — port
21214). The firmware pins G*8's npub at build time.

Assertions:
- the node's discovery line names the LEGIT endpoint (:21213)
- exactly one session; heartbeats flow with the legit daemon
- the rogue daemon never promotes a peer (a mis-pinned node would have
  completed a full IK handshake with it — its log stays silent)

Run:
    pytest tests/test_mdns_pinned.py -v
"""

import json

import pytest

from fips_lab import bench



S3_LAB_SERIAL = "F4:12:FA:CF:03:84"
LEGIT_NPUB = (
    "022f01e5e15cca351daff3843fb70f3c2f0a1bdd05e5af888a67784ef3e10a2a01"
)
LEGIT_PORT = 21213
ROGUE_MUL = 20  # unregistered multiplier: a key no device pins
ROGUE_PORT = 21214


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.timeout(420)
def test_mdns_pinned_rejects_rogue_advert(request):
    skip = bench.bench_available(S3_LAB_SERIAL)
    if skip:
        pytest.skip(skip)

    run_dir = bench.make_run_dir("mdns-pinned")
    lock = bench.acquire_board_lock()
    tap = None
    legit = None
    rogue = None
    try:
        binary = bench.build_firmware(
            bench.MICROFIPS_REPO,
            npub_hex=LEGIT_NPUB,
            nsec_hex="00" * 31 + "09",
        )

        legit = bench.LabDaemon(
            bench.MICROFIPS_REPO, 3600, run_dir / "legit",
            generator_mul=8, port=LEGIT_PORT,
        )
        rogue = bench.LabDaemon(
            bench.MICROFIPS_REPO, 3600, run_dir / "rogue",
            generator_mul=ROGUE_MUL, port=ROGUE_PORT,
        )
        legit.start()
        rogue.start()

        port = bench.find_board(serial=S3_LAB_SERIAL)
        bench.flash(port, binary)
        tap = bench.ConsoleTap(port, run_dir / "console.log")

        tap.wait_for("handshake ok", timeout=90)
        tap.wait_for("heartbeat received", timeout=60)
        # The pinning decision itself: discovery names the legit endpoint.
        tap.wait_for(f"discovered at", timeout=10)

        console = tap.read()
        legit_log = legit.log_text()
        rogue_log = rogue.log_text()

        verdict = {
            "scenario": "mdns_pinned",
            "discovery_lines": [
                ln for ln in console.splitlines() if "discovered at" in ln
            ],
            "handshake_ok_count": console.count("handshake ok"),
            "heartbeats": console.count("heartbeat received"),
            "legit_promotions": legit_log.count("Connection promoted to active peer"),
            # Node-specific: the rogue must never see the NODE's identity at
            # all (not even a handshake attempt). The node's npub is taken
            # from the legit daemon's own promotion lines — the only peer it
            # promotes in this scenario is the node. Any OTHER daemon on the
            # LAN (e.g. the workstation's system daemon) may legitimately
            # peer with the rogue; excluding by identity, not by counting.
            "node_npub": _node_npub_from_legit(legit_log, _own_npub(legit_log)),
            "rogue_mentions_node": 0,
        }
        node_npub = verdict["node_npub"]
        if node_npub:
            verdict["rogue_mentions_node"] = sum(
                1 for ln in rogue_log.splitlines() if node_npub in ln
            )
        (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

        disc = " ".join(verdict["discovery_lines"])
        assert f":{LEGIT_PORT}" in disc, f"node did not pin the legit endpoint: {disc}"
        assert f":{ROGUE_PORT}" not in disc, f"rogue endpoint surfaced: {disc}"
        assert verdict["handshake_ok_count"] == 1, verdict
        assert verdict["heartbeats"] >= 1, verdict
        assert verdict["legit_promotions"] >= 1, verdict
        # The sharp assertion: a mis-pinned node completes a full handshake
        # with the rogue (the lab ACL is default-open) — the rogue never
        # even sees the node's identity. Asserted only when the npub was
        # extractable (an empty extraction fails loudly below).
        assert verdict["node_npub"], "could not extract the node npub from the legit log"
        assert verdict["rogue_mentions_node"] == 0, verdict
    finally:
        if tap:
            tap.stop()
        if rogue:
            rogue.stop(restore=False)
        if legit:
            legit.stop()
        lock.release()


def _own_npub(daemon_log: str) -> str:
    """The daemon's own bech32 npub from its startup line."""
    for ln in daemon_log.splitlines():
        if "npub:" in ln:
            return ln.split("npub:", 1)[1].strip().split()[0]
    return ""


def _node_npub_from_legit(legit_log: str, legit_own_npub: str) -> str:
    """The bench node's npub: the peer the legit daemon promoted that is not
    itself (its only node peer in this scenario)."""
    for ln in legit_log.splitlines():
        if "promoted to active peer" in ln and "peer=npub" in ln:
            peer = ln.split("peer=", 1)[1].split()[0]
            if peer != legit_own_npub:
                return peer
    return ""
