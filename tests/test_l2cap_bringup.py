"""L2CAP bring-up: atom-a (ESP32-D0WD) peers directly with the lab daemon
over BLE L2CAP — audit #188 candidate 4, bench-era test_esp32_l2cap.py
retired in its favor.

Topology: atom runs `microfips-esp32-l2cap` (G*11 identity), lab daemon
(G*8) started with the BLE transport on hci0. The atom's ONLY peer pin is
FIPS_EXTRA_ALLOWED_XONLY_HEX (the L2CAP host validates the exchanged
daemon pubkey against it — DEVICE_NPUB_HEX_vps is not compiled into this
transport). The system daemon also runs a BLE transport and holds the
hci0 advertisement slot, so the lab daemon cannot advertise; the link
forms via the lab daemon's scanner probing the atom's peripheral advert
(observed on hardware: `BLE connection accepted` -> `L2CAP channel
accepted on PSM 133`). A probe from the system daemon would be REJECTED
by the atom's pin — `rejecting` console lines are soft-recorded.

Bench quirks encoded here (validated 2026-09-02):
- OPENING the atom's FTDI port asserts DTR and RESETS the board
  (auto-reset circuit). The tap therefore starts a deterministic fresh
  boot AFTER the daemon is up — relied on, not worked around.
- With no FIPS peer in range the L2CAP retry loop is INFO-quiet: an
  idle console is expected until the daemon connects. Don't diagnose
  silence without checking the daemon side first.

Hard assertions: pubkey exchange + IK handshake on the atom console,
sustained heartbeats, FSP initiator sends (armed 5s after handshake,
8s retries), daemon-side promotion + BLE peer discovery, zero
disconnects. Probe strings derive from the emitting source (playbook
pattern 10): microfips-protocol/node.rs, esp-transport run_tasks.rs /
l2cap_host.rs, fips io_linux.rs.

Run:
    pytest tests/test_l2cap_bringup.py -v
"""

import json
import time

import pytest

from fips_lab import bench

ATOM_A_SERIAL = "81528A13B6"
ATOM_A_MUL = 11
LAB_DAEMON_NPUB = (
    "022f01e5e15cca351daff3843fb70f3c2f0a1bdd05e5af888a67784ef3e10a2a01"
)
LAB_DAEMON_XONLY = LAB_DAEMON_NPUB[2:]


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.timeout(600)
def test_l2cap_bringup():
    skip = bench.bench_available(ATOM_A_SERIAL, vidpid=bench.D0WD_VIDPID)
    if skip:
        pytest.skip(skip)

    binary = bench.build_d0wd_l2cap(
        bench.MICROFIPS_REPO,
        nsec_hex=bench.lab_nsec(bench.MICROFIPS_REPO, ATOM_A_MUL),
        extra_allowed_xonly_hex=LAB_DAEMON_XONLY,
    )

    run_dir = bench.make_run_dir("l2cap-bringup")
    lock = bench.acquire_board_lock()
    tap = None
    daemon = None
    with bench.reserve_board(ATOM_A_SERIAL):
        try:
            # Daemon first: the node boots straight out of flash and starts
            # its BLE scan/advertising cycle — the listener must be up.
            daemon = bench.LabDaemon(
                bench.MICROFIPS_REPO, 3600, run_dir / "daemon", ble=True,
            )
            daemon.start()

            # One-advert bench: atom-b (or any other attached atom) must be
            # radio-silent or its central scan steals this atom's single BLE
            # connection (two-atom interference, 2026-09-03).
            bench.quiesce_peer_radios(bench.MICROFIPS_REPO, ATOM_A_SERIAL)

            port = bench.find_board(
                vidpid=bench.D0WD_VIDPID, serial=ATOM_A_SERIAL,
            )
            assert port is not None, "atom-a FTDI port vanished mid-scenario"
            bench.flash(port, binary, chip="esp32")
            tap = bench.ConsoleTap(port, run_dir / "console-atom.log", baud=115200)

            # BLE bring-up: init + central scan (3s) or peripheral advert,
            # daemon probe (30s cooldown per address), L2CAP channel,
            # pubkey exchange. 180s covers several retry cycles incl.
            # system-daemon probes the pin must reject first.
            tap.wait_for("pubkey exchange complete", timeout=180)
            tap.wait_for("handshake ok", timeout=60)

            # Sustained link: node heartbeat cadence ~10s.
            tap.wait_for("heartbeat received from peer", count=2, timeout=90)

            # FSP initiator: arms 5s after handshake, retries every 8s —
            # one 25s settle covers several attempts.
            time.sleep(25)

            console = tap.read()
            daemon_log = daemon.log_text()
            verdict = {
                "scenario": "l2cap_bringup",
                "handshake_ok": console.count("handshake ok"),
                "peer_pubkey_accepted": console.count(
                    f"peer x-only pubkey: {LAB_DAEMON_XONLY[:24]}"
                ),
                "heartbeats_rx": console.count("heartbeat received"),
                "session_datagram_sends": console.count(
                    "sending session datagram"
                ),
                "datagram_recvs": console.count("fsp: datagram in"),
                "central_connected": console.count(
                    "central BLE connection established"
                ),
                "peripheral_accepted": console.count("BLE connection accepted"),
                "foreign_peer_rejects": console.count("rejecting"),
                "daemon_ble_peer_found": daemon_log.count(
                    "BLE scanner: FIPS peer found"
                ),
                "daemon_promotions": daemon_log.count("Connection promoted"),
                "daemon_disconnects": daemon_log.count("disconnect notification"),
            }
            (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

            assert verdict["handshake_ok"] >= 1, verdict
            assert verdict["heartbeats_rx"] >= 2, verdict
            assert verdict["session_datagram_sends"] >= 1, verdict
            assert verdict["daemon_ble_peer_found"] >= 1, verdict
            assert verdict["daemon_promotions"] >= 1, verdict
            assert verdict["daemon_disconnects"] == 0, verdict
        finally:
            if tap:
                tap.stop()
            if daemon:
                daemon.stop()
            lock.release()
