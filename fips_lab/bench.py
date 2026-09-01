"""microfips bench: verified builds, flashing, no-reset tap, lab-daemon lifecycle.

The reusable layer under every WiFi-path bench scenario
(docs/bench-testing-playbook.md). Patterns encoded here so they stop being
manual discipline:

- board identity by VID:PID + serial (never ttyN)
- build-env hygiene (clean with profile+target) + binary verification of
  compiled-in values (SSID / pinned npub / nsec) BEFORE flashing
- port kill order: reader first, then fuser, then flash
- no-reset console tap (fips_lab.raw_tap as a detached subprocess)
- isolated lab-daemon lifecycle with config override + exact-PID kills
- results/<run_id>/ artifact retention
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from tollgate_lab import HardwareLock

MICROFIPS_REPO = Path(os.environ.get("MICROFIPS_REPO", "~/src/microfips")).expanduser()
FIPS_BIN = Path(os.environ.get("FIPS_BIN", "~/src/fips/target/release/fips")).expanduser()
EXPORT_ESP = Path(os.environ.get("EXPORT_ESP", "~/export-esp.sh")).expanduser()
S3_TARGET = "xtensa-esp32s3-none-elf"
S3_VIDPID = "303a/1001"
RESULTS_ROOT = Path(__file__).resolve().parent.parent / "results"
TAP_SCRIPT = Path(__file__).resolve().parent / "raw_tap.py"


def find_board(vidpid: str = S3_VIDPID, serial: str | None = None) -> Path | None:
    """Locate a USB-serial board by sysfs PRODUCT (+ optional serial)."""
    for prefix in ("ttyACM", "ttyUSB"):
        for p in Path("/dev").glob(prefix + "*"):
            try:
                uevent = Path("/sys/class/tty", p.name, "device/../uevent").read_text()
            except OSError:
                continue
            m = re.search(r"^PRODUCT=(\S+)", uevent, re.M)
            if not m or m.group(1).split("/")[:2] != vidpid.split("/")[:2]:
                continue
            if serial is None:
                return p
            props = subprocess.run(
                ["udevadm", "info", "-q", "property", str(p)],
                capture_output=True, text=True,
            ).stdout
            if f"ID_SERIAL_SHORT={serial}" in props:
                return p
    return None


def load_dotenv(repo: Path) -> dict[str, str]:
    """Line-wise .env loader (comments/blank lines skipped — the naive
    `export $(grep ...)` splits SSIDs with spaces and chokes on comments)."""
    env = {}
    env_file = repo / ".env"
    if not env_file.exists():
        return env
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k] = v
    return env


def lab_daemon_nsec(repo: Path, generator_mul: int = 8) -> str:
    out = subprocess.check_output(
        [sys.executable, str(repo / "tools/lab_keygen.py"), str(generator_mul)],
        text=True,
    )
    return json.loads(out)["nsec_hex"]


def build_firmware(repo: Path, npub_hex: str, nsec_hex: str) -> Path:
    """Env-pinned release build with cargo-clean hygiene and binary
    verification. Raises RuntimeError on any verification miss — never
    debug a stale pin on hardware (playbook pattern #4)."""
    wifi = load_dotenv(repo)
    if "WIFI_SSID" not in wifi or "WIFI_PASSWORD" not in wifi:
        raise RuntimeError("microfips .env missing WIFI_SSID/WIFI_PASSWORD")

    subprocess.run(
        ["cargo", "clean", "-p", "microfips-esp-transport", "-p", "microfips-esp32s3",
         "--release", "--target", S3_TARGET],
        cwd=repo, check=True, capture_output=True,
    )

    env = dict(os.environ)
    env.update({
        "WIFI_SSID": wifi["WIFI_SSID"],
        "WIFI_PASSWORD": wifi["WIFI_PASSWORD"],
        "DEVICE_NPUB_HEX_vps": npub_hex,
        "DEVICE_NSEC_HEX_esp32s3": nsec_hex,
        "RUSTUP_TOOLCHAIN": "esp",
    })
    cmd = (
        f". {EXPORT_ESP} && RUSTUP_TOOLCHAIN=esp cargo build "
        f"-p microfips-esp32s3 --release --target {S3_TARGET} "
        f"-Zbuild-std=core,alloc"
    )
    subprocess.run(["bash", "-c", cmd], cwd=repo, check=True, capture_output=True, env=env)

    binary = repo / "target" / S3_TARGET / "release" / "microfips-esp32s3"
    data = binary.read_bytes()
    misses = []
    if wifi["WIFI_SSID"].encode() not in data:
        misses.append("WIFI_SSID")
    if bytes.fromhex(npub_hex) not in data:
        misses.append("pinned npub")
    if bytes.fromhex(nsec_hex) not in data:
        misses.append("device nsec")
    if misses:
        raise RuntimeError(f"binary verification failed (stale pin?): missing {misses}")
    return binary


def flash(port: Path, binary: Path, esp_toolchain: Path = EXPORT_ESP) -> None:
    """Flash with port-lifecycle discipline: reader first, then fuser."""
    subprocess.run(
        ["pkill", "-f", f"raw_tap.py {port}"], capture_output=True,
    )
    time.sleep(0.5)
    subprocess.run(["fuser", "-k", str(port)], capture_output=True)
    time.sleep(1)
    cmd = f". {esp_toolchain} && espflash flash -p {port} --chip esp32s3 {binary}"
    subprocess.run(
        ["bash", "-c", cmd], check=True, capture_output=True, timeout=150,
    )


class ConsoleTap:
    """Detached no-reset console tap; read() returns decoded text so far."""

    def __init__(self, port: Path, outfile: Path):
        self.outfile = outfile
        self._proc = subprocess.Popen(
            [sys.executable, str(TAP_SCRIPT), str(port), str(outfile)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def read(self) -> str:
        try:
            return self.outfile.read_text(errors="replace")
        except OSError:
            return ""

    def wait_for(self, needle: str, count: int = 1, timeout: float = 90.0) -> float:
        """Block until `needle` appears >= count times; returns elapsed s."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.read().count(needle) >= count:
                return timeout - (deadline - time.monotonic())
            time.sleep(1.0)
        raise TimeoutError(
            f"console: {needle!r} x{count} not seen in {timeout}s "
            f"(got {self.read().count(needle)})"
        )

    def stop(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()


class LabDaemon:
    """Isolated lab daemon with a scenario config; restores the standard
    lab daemon on exit if one was running (run_lab_daemon.sh semantics:
    exact-PID kills, setsid start, derived G*8 identity)."""

    STANDARD_CONFIG = Path("/tmp/opencode/fips-lab.yaml")

    def __init__(self, repo: Path, rekey_after_secs: int, workdir: Path):
        self.repo = repo
        self.rekey_after_secs = rekey_after_secs
        self.workdir = workdir
        self.config = workdir / "daemon.yaml"
        self.log = workdir / "daemon.log"
        self._proc = None
        self._standard_was_running = False

    def _pids_for(self, config: Path) -> list[int]:
        out = subprocess.run(
            ["pgrep", "-f", f"fips --config {config}"], capture_output=True, text=True,
        ).stdout
        return [int(x) for x in out.split()]

    def start(self) -> None:
        # Stop the standard lab daemon (same bind address) and remember it.
        # Wait for the PID to actually exit — a 2s sleep races graceful
        # shutdown and the new daemon then fails to bind (2026-09-01).
        std_pids = self._pids_for(self.STANDARD_CONFIG)
        self._standard_was_running = bool(std_pids)
        for pid in std_pids:
            subprocess.run(["kill", str(pid)], capture_output=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self._pids_for(self.STANDARD_CONFIG):
            time.sleep(0.5)

        template = (self.repo / "tools/fips-lab.yaml").read_text()
        nsec = lab_daemon_nsec(self.repo)
        cfg = template.replace("__LAB_DAEMON_NSEC__", nsec)
        # Scenario knob: speed the rekey cadence (node.rekey.after_secs).
        cfg = cfg.replace(
            "transports:",
            f"  rekey:\n    after_secs: {self.rekey_after_secs}\ntransports:",
        )
        self.config.write_text(cfg)
        self.log.write_text("")

        self._proc = subprocess.Popen(
            [str(FIPS_BIN), "--config", str(self.config)],
            stdout=self.log.open("ab"), stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        # Readiness = the UDP transport is bound ("npub:" alone fires
        # before transports start and can precede a startup failure).
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            log = self.log.read_text(errors="replace")
            if "UDP transport started" in log:
                return
            if "Failed to start node" in log or self._proc.poll() is not None:
                raise RuntimeError(f"lab daemon failed: {log[-600:]}")
            time.sleep(0.5)
        raise TimeoutError(f"lab daemon transport not up: {self.log.read_text()[-600:]}")

    def log_text(self) -> str:
        return self.log.read_text(errors="replace")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._standard_was_running:
            subprocess.run(
                ["bash", str(self.repo / "scripts/run_lab_daemon.sh")],
                capture_output=True, timeout=60,
            )


def make_run_dir(label: str) -> Path:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + label
    run_dir = RESULTS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def bench_available(board_serial: str) -> str | None:
    """Return a skip-reason if the bench can't run, else None."""
    if find_board(serial=board_serial) is None:
        return f"bench board {board_serial} not attached"
    if not (MICROFIPS_REPO / ".env").exists():
        return "microfips .env missing"
    if not FIPS_BIN.exists():
        return f"fips binary missing at {FIPS_BIN}"
    if not EXPORT_ESP.exists():
        return f"esp toolchain export missing at {EXPORT_ESP}"
    return None


def acquire_board_lock(name: str = "microfips-bench"):
    """Cross-project hardware lock (tollgate-lab)."""
    lock = HardwareLock(name)
    lock.acquire()
    return lock
