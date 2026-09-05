"""Lab daemon hygiene (fips-lab #9): stale scenario daemons must never
squat the shared lab UDP bind silently.

2026-09-05: a lab daemon left over from an interrupted hil-smoke leg held
192.168.13.221:21213 for 14+ h (on a deleted binary inode); the next
bench-nightly failed all three scenarios three layers deep
("no operational transports" -> daemon log tail -> EADDRINUSE). These
tests pin the hardening: the holder is RESOLVED and NAMED before any
spawn, and the sweep kills only provably-stale scenario daemons
(config under a results/ dir) — never the system daemon or anything
that doesn't match the pattern.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fips_lab import bench


def _hold_udp(ip: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((ip, port))
    return s


def test_udp_port_holder_resolves_bound_socket():
    s = _hold_udp("127.0.0.1", 0)
    port = s.getsockname()[1]
    try:
        holder = bench._udp_port_holder("127.0.0.1", port)
        assert holder is not None, "holder must resolve while the socket is bound"
        pid, name = holder
        assert pid == os.getpid(), f"expected our own pid, got {pid}"
        assert name, "process name must be non-empty"
    finally:
        s.close()


def test_udp_port_holder_none_when_free():
    s = _hold_udp("127.0.0.1", 0)
    port = s.getsockname()[1]
    s.close()
    assert bench._udp_port_holder("127.0.0.1", port) is None


def test_lab_daemon_preflight_names_the_holder():
    """A held bind must fail LabDaemon.start() BEFORE spawning, with the
    holder pid + cmdline in the error (the actionable version of
    EADDRINUSE — today it surfaces as a daemon log tail three layers
    deep)."""
    s = _hold_udp("127.0.0.1", 0)
    port = s.getsockname()[1]
    try:
        daemon = bench.LabDaemon(
            Path(tempfile.mkdtemp()), 3600,
            Path(tempfile.mkdtemp()) / "daemon",
            port=port, bind_ip="127.0.0.1",
        )
        try:
            daemon.start()
        except RuntimeError as e:
            msg = str(e)
            assert str(os.getpid()) in msg, f"holder pid missing from: {msg}"
            assert "127.0.0.1" in msg and str(port) in msg, f"bind missing from: {msg}"
        else:
            raise AssertionError("start() must refuse when the bind is held")
        finally:
            daemon.stop(restore=False)
    finally:
        s.close()


def _stale_holder(port: int, results_cfg: bool) -> subprocess.Popen:
    """A fake holder whose cmdline matches (or not) the stale-scenario
    pattern: 'fips --config <...>/results/<...>/daemon.yaml'."""
    cfg_dir = Path(tempfile.mkdtemp(prefix="20269999-000000-fake-")) / "results" / "run"
    if not results_cfg:
        cfg_dir = Path(tempfile.mkdtemp(prefix="not-results-")) / "run"
    cfg_dir.mkdir(parents=True)
    argv = [
        sys.executable, "-c",
        "import socket, time; "
        f"s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); "
        f"s.bind(('127.0.0.1', {port})); "
        "print('holding', flush=True); time.sleep(600)",
        "fips", "--config", str(cfg_dir / "daemon.yaml"),
    ]
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if bench._udp_port_holder("127.0.0.1", port):
            return p
        time.sleep(0.1)
    raise AssertionError("fake holder never bound")


def test_kill_stale_lab_daemon_reaps_scenario_daemon():
    s = _hold_udp("127.0.0.1", 0)
    port = s.getsockname()[1]
    s.close()
    holder = _stale_holder(port, results_cfg=True)
    try:
        killed = bench.kill_stale_lab_daemon("127.0.0.1", port)
        assert holder.pid in killed, f"expected {holder.pid} reaped, got {killed}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and holder.poll() is None:
            time.sleep(0.1)
        assert holder.poll() is not None, "holder must be dead after the sweep"
    finally:
        if holder.poll() is None:
            holder.kill()


def test_kill_stale_lab_daemon_refuses_non_scenario_holders():
    """Anything that is not a fips scenario daemon (config under
    results/) must be LEFT ALONE — the sweep never kills blind."""
    s = _hold_udp("127.0.0.1", 0)
    port = s.getsockname()[1]
    s.close()
    holder = _stale_holder(port, results_cfg=False)
    try:
        try:
            bench.kill_stale_lab_daemon("127.0.0.1", port)
        except RuntimeError as e:
            assert str(holder.pid) in str(e), "refusal must name the holder"
        else:
            raise AssertionError("sweep must refuse non-scenario holders")
        assert holder.poll() is None, "non-scenario holder must survive the sweep"
    finally:
        holder.kill()
