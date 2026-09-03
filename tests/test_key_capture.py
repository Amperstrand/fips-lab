"""Key capture + traffic decryption on hardware — #175 graduation.

End-to-end exercise of the btsnoop pipeline on real BLE hardware: atom-a
(ESP32-D0WD, L2CAP firmware built with the `noise-keylog` feature) peers
with the lab daemon while btmon records hci0; the node's FIPS_LINK console
lines become the keylog; lab.capture.btsnoop_decrypt decrypts the captured
session. Success = captured frames decrypted to plaintext with >= 3 link
message types identified (MSG1/MSG2 + Heartbeat + MMP reports). Every
future wire-level issue becomes a readable capture instead of guesswork.

Key source is NODE-side (microfips-protocol emits at handshake + rekey
cutover): the v0.5.0 daemon has no FIPS_NOISE_KEYLOG support, and the IK
initiator participates in every DH operation, so its k_send/k_recv pair
decrypts both directions — btsnoop_decrypt tries both orientations of
every keylog entry anyway.

btmon runs LOCALLY via passwordless sudo (monitor socket, read-only —
coexists with the system daemon's hci0 ownership and the lab daemon's
scanner). Started before the lab daemon so connection setup is captured;
stopped by process-group SIGTERM before decryption (btsnoop records are
written per-packet, SIGTERM flushes).

Run:
    pytest tests/test_key_capture.py -v
"""

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

from fips_lab import bench

ATOM_A_SERIAL = "81528A13B6"
ATOM_A_MUL = 11
LAB_DAEMON_NPUB = (
    "022f01e5e15cca351daff3843fb70f3c2f0a1bdd05e5af888a67784ef3e10a2a01"
)
LAB_DAEMON_XONLY = LAB_DAEMON_NPUB[2:]

# Same grammar lab/capture/keylog.py parses: kind + 4 x 64 lowercase hex.
KEYLOG_LINE_RE = re.compile(
    r"FIPS_LINK ([0-9a-f]{64}) ([0-9a-f]{64}) ([0-9a-f]{64}) ([0-9a-f]{64})"
)


class LocalBtmon:
    """Local `sudo -n btmon -i hci0 -w <run_dir>/btmon.btsnoop` capture.

    Own process group (start_new_session) so SIGTERM reaches btmon through
    sudo; the exact-name `pkill -x btmon` safety net only ever matches the
    capture process itself, never this shell (AGENTS.md pkill -f rule).
    """

    def __init__(self, run_dir: Path, adapter: str = "hci0"):
        self.path = run_dir / "btmon.btsnoop"
        self._proc = subprocess.Popen(
            ["sudo", "-n", "btmon", "-i", adapter, "-w", str(self.path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        time.sleep(1.5)
        if self._proc.poll() is not None:
            raise RuntimeError("btmon exited immediately — is another btmon running?")
        if not self.path.exists():
            raise RuntimeError("btmon did not create the btsnoop file")

    def stop(self) -> None:
        if self._proc.poll() is None:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                self._proc.wait(timeout=5)
        subprocess.run(
            ["sudo", "-n", "pkill", "-x", "btmon"], capture_output=True,
        )


@pytest.mark.hardware
@pytest.mark.flash
@pytest.mark.timeout(900)
def test_l2cap_key_capture():
    skip = bench.bench_available(ATOM_A_SERIAL, vidpid=bench.D0WD_VIDPID)
    if skip:
        pytest.skip(skip)

    binary = bench.build_d0wd_l2cap(
        bench.MICROFIPS_REPO,
        nsec_hex=bench.lab_nsec(bench.MICROFIPS_REPO, ATOM_A_MUL),
        extra_allowed_xonly_hex=LAB_DAEMON_XONLY,
        keylog=True,
    )

    run_dir = bench.make_run_dir("key-capture")
    lock = bench.acquire_board_lock()
    tap = None
    daemon = None
    capture = None
    with bench.reserve_board(ATOM_A_SERIAL):
        try:
            # Capture first, daemon second: the scanner's connection setup
            # and the L2CAP channel open belong in the btsnoop too.
            capture = LocalBtmon(run_dir)
            daemon = bench.LabDaemon(
                bench.MICROFIPS_REPO, 3600, run_dir / "daemon", ble=True,
            )
            daemon.start()

            port = bench.find_board(
                vidpid=bench.D0WD_VIDPID, serial=ATOM_A_SERIAL,
            )
            assert port is not None, "atom-a FTDI port vanished mid-scenario"
            bench.flash(port, binary, chip="esp32")
            tap = bench.ConsoleTap(port, run_dir / "console-atom.log", baud=115200)

            tap.wait_for("pubkey exchange complete", timeout=180)
            tap.wait_for("handshake ok", timeout=60)

            # Sustained link: 3 heartbeats (~10s cadence) gives the capture
            # both directions plus MMP reports; +25s settle covers FSP
            # initiator sends (armed 5s after handshake, 8s retries).
            tap.wait_for("heartbeat received from peer", count=3, timeout=120)
            time.sleep(25)

            console = tap.read()
            keylog_lines = KEYLOG_LINE_RE.findall(console)
            assert keylog_lines, (
                "no FIPS_LINK lines on the console — noise-keylog feature "
                "not in the flashed binary?"
            )
            for local, peer, _ks, _kr in keylog_lines:
                assert peer == LAB_DAEMON_XONLY, (
                    f"keylog peer field {peer} is not the pinned lab daemon"
                )
            keylog_path = run_dir / "keylog-atom.txt"
            keylog_path.write_text(
                "\n".join(
                    f"FIPS_LINK {local} {peer} {ks} {kr}"
                    for local, peer, ks, kr in keylog_lines
                ) + "\n"
            )
        finally:
            if tap:
                tap.stop()
            if capture:
                capture.stop()
            if daemon:
                daemon.stop()
            lock.release()

    # Decryption after teardown: btmon.btsnoop + keylog-atom.txt are on
    # disk, decrypt_btsnoop_capture writes decryption-summary.{json,md}
    # and a decrypted pcapng into the run dir.
    from lab.capture.btsnoop_decrypt import decrypt_btsnoop_capture

    summary = decrypt_btsnoop_capture(run_dir)
    assert summary is not None, (
        "decrypt pipeline returned None — btsnoop or keylog not found in "
        f"{run_dir}"
    )

    types_seen = {
        name for name, entry in summary.link_messages.items()
        if entry["count"] > 0
    }
    verdict = {
        "scenario": "l2cap_key_capture",
        "keylog_lines": len(keylog_lines),
        "fmp_msg1": summary.fmp_frames["handshake_msg1"],
        "fmp_msg2": summary.fmp_frames["handshake_msg2"],
        "fmp_established": summary.fmp_frames["established"],
        "decrypted": summary.decryption["decrypted_successfully"],
        "decryption_failed": summary.decryption["decryption_failed"],
        "failure_pct": summary.decryption["failure_pct"],
        "message_types": sorted(types_seen),
        "run_dir": str(run_dir),
    }
    (run_dir / "verdict.json").write_text(json.dumps(verdict, indent=2))

    # #175 success criteria: handshake frames identified, >= 3 message
    # types decrypted (MSG1/MSG2 parse in the clear; Heartbeat + MMP
    # reports + SessionDatagrams only readable with correct keys).
    assert verdict["fmp_msg1"] >= 1, verdict
    assert verdict["fmp_msg2"] >= 1, verdict
    assert verdict["decrypted"] >= 5, verdict
    assert "Heartbeat" in types_seen, verdict
    assert len(types_seen) >= 3, verdict
    assert verdict["failure_pct"] < 50.0, verdict
