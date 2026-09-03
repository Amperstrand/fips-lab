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
from tollgate_lab.reservation import BoardReservation

MICROFIPS_REPO = Path(os.environ.get("MICROFIPS_REPO", "~/src/microfips")).expanduser()
BOARDS_TOML = Path(__file__).resolve().parent / "boards.toml"
FIPS_BIN = Path(os.environ.get("FIPS_BIN", "~/src/fips/target/release/fips")).expanduser()
EXPORT_ESP = Path(os.environ.get("EXPORT_ESP", "~/export-esp.sh")).expanduser()
S3_TARGET = "xtensa-esp32s3-none-elf"
S3_VIDPID = "303a/1001"
RESULTS_ROOT = Path(__file__).resolve().parent.parent / "results"
TAP_SCRIPT = Path(__file__).resolve().parent / "raw_tap.py"
FTDI_TAP_SCRIPT = Path(__file__).resolve().parent / "ftdi_tap.py"


def _vidpid_match(product: str, vidpid: str) -> bool:
    """sysfs PRODUCT strips leading zeros ('403/6001' for FTDI 0403:6001) —
    compare as hex ints, not strings."""
    try:
        return [int(x, 16) for x in product.split("/")[:2]] == \
               [int(x, 16) for x in vidpid.split("/")[:2]]
    except ValueError:
        return False


def find_board(vidpid: str = S3_VIDPID, serial: str | None = None) -> Path | None:
    """Locate a USB-serial board by sysfs PRODUCT (+ optional serial)."""
    for prefix in ("ttyACM", "ttyUSB"):
        for p in Path("/dev").glob(prefix + "*"):
            try:
                uevent = Path("/sys/class/tty", p.name, "device/../uevent").read_text()
            except OSError:
                continue
            m = re.search(r"^PRODUCT=(\S+)", uevent, re.M)
            if not m or not _vidpid_match(m.group(1), vidpid):
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


class BoardError(RuntimeError):
    """A board operation was refused by the boards.toml safety contract."""


def require_board(serial: str, op: str) -> None:
    """Safety gate (bolty-rs cards.toml pattern): the board must be listed
    in boards.toml with `op` permitted, or the operation is REFUSED. This
    is what keeps off-limits hardware (e.g. the M5 Stack) out of every
    scenario by construction — no scenario can flash what the registry
    does not allow."""
    import tomllib

    with open(BOARDS_TOML, "rb") as f:
        data = tomllib.load(f)
    entry = data.get("boards", {}).get(serial)
    if entry is None:
        raise BoardError(
            f"board {serial!r} is not in {BOARDS_TOML.name} — refusing {op}. "
            "Add an entry with explicit ops to allow it."
        )
    if op not in entry.get("ops", []):
        raise BoardError(
            f"board {serial!r} ({entry.get('alias', '?')}) does not permit "
            f"'{op}' (allowed: {', '.join(entry.get('ops', []))})"
        )


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


def lab_nsec(repo: Path, generator_mul: int) -> str:
    """Deterministic bench identity scalar (G*N) for any generator — the
    board identities (atom-a G*11, atom-b G*12, s3-lab G*9, ...) and the
    lab daemon (G*8) all come from the same tools/lab_keygen.py."""
    out = subprocess.check_output(
        [sys.executable, str(repo / "tools/lab_keygen.py"), str(generator_mul)],
        text=True,
    )
    return json.loads(out)["nsec_hex"]


def lab_npub(repo: Path, generator_mul: int) -> str:
    """Compressed npub (66 hex) for a G*N bench identity — the pin target
    for firmware built against a lab daemon (e.g. the XX daemon G*22)."""
    out = subprocess.check_output(
        [sys.executable, str(repo / "tools/lab_keygen.py"), str(generator_mul)],
        text=True,
    )
    return json.loads(out)["npub_hex"]


def lab_daemon_nsec(repo: Path, generator_mul: int = 8) -> str:
    return lab_nsec(repo, generator_mul)


def build_firmware(
    repo: Path, npub_hex: str, nsec_hex: str, extra_env: dict[str, str] | None = None,
    features: str | None = None,
) -> Path:
    """Env-pinned release build with cargo-clean hygiene and binary
    verification. Raises RuntimeError on any verification miss — never
    debug a stale pin on hardware (playbook pattern #4). `extra_env`
    adds compiled-in knobs (e.g. REKEY_AFTER_SECS). Note: knobs that
    compile to non-string constants can't be binary-checked here —
    verify those behaviorally via a boot/console signature, or via
    `verify_knob` when a compile-time-distinct string exists.
    `features` selects cargo feature flags (e.g. "noise-xx" for the XX
    wire — its presence is binary-verifiable via the negotiation log
    string, which only compiles under that feature)."""
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
    env.update(extra_env or {})
    feature_flag = f" --features {features}" if features else ""
    cmd = (
        f". {EXPORT_ESP} && RUSTUP_TOOLCHAIN=esp cargo build "
        f"-p microfips-esp32s3 --release --target {S3_TARGET} "
        f"-Zbuild-std=core,alloc{feature_flag}"
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
    if features and "noise-xx" in features and \
            b"fmp negotiation: agreed version" not in data:
        misses.append("noise-xx negotiation marker")
    if misses:
        raise RuntimeError(f"binary verification failed (stale pin?): missing {misses}")
    return binary


def verify_knob(binary: Path, marker: str) -> None:
    """Binary-check a compiled-in string-valued knob (e.g. a build-time
    env override whose value is embedded as text). Numeric `option_env!`
    knobs compile to constants — verify those behaviorally instead."""
    if marker.encode() not in binary.read_bytes():
        raise RuntimeError(
            f"knob marker {marker!r} missing from binary — stale build"
        )


def flash(port: Path, binary: Path, esp_toolchain: Path = EXPORT_ESP,
          chip: str = "esp32s3") -> None:
    """Flash with port-lifecycle discipline: reader first, then fuser.
    Refuses boards not registered for 'flash' in boards.toml. `chip`
    selects the espflash target (esp32s3 default; esp32 for the D0WD
    atoms)."""
    serial = _serial_of(port)
    if serial:
        require_board(serial, "flash")
    for tap_name in ("raw_tap.py", "ftdi_tap.py"):
        subprocess.run(
            ["pkill", "-f", f"{tap_name} {port}"], capture_output=True,
        )
    time.sleep(0.5)
    subprocess.run(["fuser", "-k", str(port)], capture_output=True)
    time.sleep(1)
    cmd = f". {esp_toolchain} && espflash flash -p {port} --chip {chip} {binary}"
    subprocess.run(
        ["bash", "-c", cmd], check=True, capture_output=True, timeout=150,
    )


STM32_VIDPID = "c0de/cafe"
ARM_TARGET = "thumbv7em-none-eabi"

D0WD_TARGET = "xtensa-esp32-none-elf"
D0WD_VIDPID = "0403/6001"  # FTDI — atoms AND the off-limits M5 Stack; serial disambiguates
D0WD_L2CAP_BIN = "microfips-esp32-l2cap"
D0WD_UART_BIN = "microfips-esp32"


def build_d0wd_l2cap(
    repo: Path, nsec_hex: str, extra_allowed_xonly_hex: str,
    keylog: bool = False,
) -> Path:
    """D0WD L2CAP tier (audit #188 candidate 4): build `microfips-esp32`
    with `--features l2cap`. No WiFi env — the L2CAP transport never
    associates. Pinning note (differs from the WiFi tier): the L2CAP host
    learns the daemon pubkey from the BLE exchange and validates it against
    FIPS_ALLOWED_PUBKEYS + FIPS_EXTRA_ALLOWED_XONLY_HEX (esp-transport
    config.rs) — DEVICE_NPUB_HEX_vps is never compiled in, so the ONLY
    peer pin is the allowlist knob. It is consumed via option_env! and
    embedded as lowercase ASCII hex (compared at runtime against a
    hex_encode of the exchanged key), so that is the form to verify in
    the binary (playbook pattern 4).

    `keylog=True` adds the `noise-keylog` feature (#175): the node logs
    `FIPS_LINK` transport-key lines at handshake + rekey cutover for the
    btsnoop_decrypt pipeline. LEAKS link keys — bench builds only."""
    subprocess.run(
        ["cargo", "clean", "-p", "microfips-esp-transport", "-p", "microfips-esp32",
         "--release", "--target", D0WD_TARGET],
        cwd=repo, check=True, capture_output=True,
    )

    env = dict(os.environ)
    env.update({
        "DEVICE_NSEC_HEX_esp32": nsec_hex,
        "FIPS_EXTRA_ALLOWED_XONLY_HEX": extra_allowed_xonly_hex,
        "RUSTUP_TOOLCHAIN": "esp",
    })
    features = "l2cap,noise-keylog" if keylog else "l2cap"
    cmd = (
        f". {EXPORT_ESP} && RUSTUP_TOOLCHAIN=esp cargo build "
        f"-p microfips-esp32 --release --target {D0WD_TARGET} "
        f"-Zbuild-std=core,alloc --features {features} --bin {D0WD_L2CAP_BIN}"
    )
    subprocess.run(["bash", "-c", cmd], cwd=repo, check=True, capture_output=True, env=env)

    binary = repo / "target" / D0WD_TARGET / "release" / D0WD_L2CAP_BIN
    data = binary.read_bytes()
    misses = []
    if extra_allowed_xonly_hex.encode() not in data:
        misses.append("extra allowlist xonly (ascii)")
    if bytes.fromhex(nsec_hex) not in data:
        misses.append("device nsec")
    if keylog and b"FIPS_LINK" not in data:
        misses.append("FIPS_LINK wiretap marker (noise-keylog feature stale?)")
    if misses:
        raise RuntimeError(f"binary verification failed (stale pin?): missing {misses}")
    return binary


def build_d0wd_uart(repo: Path, nsec_hex: str) -> Path:
    """D0WD UART tier: the default (radio-silent) `microfips-esp32` build —
    no BLE, no WiFi. The quiesce image for peer atoms during L2CAP legs."""
    subprocess.run(
        ["cargo", "clean", "-p", "microfips-esp-transport", "-p", "microfips-esp32",
         "--release", "--target", D0WD_TARGET],
        cwd=repo, check=True, capture_output=True,
    )

    env = dict(os.environ)
    env.update({
        "DEVICE_NSEC_HEX_esp32": nsec_hex,
        "RUSTUP_TOOLCHAIN": "esp",
    })
    cmd = (
        f". {EXPORT_ESP} && RUSTUP_TOOLCHAIN=esp cargo build "
        f"-p microfips-esp32 --release --target {D0WD_TARGET} "
        f"-Zbuild-std=core,alloc --bin {D0WD_UART_BIN}"
    )
    subprocess.run(["bash", "-c", cmd], cwd=repo, check=True, capture_output=True, env=env)

    binary = repo / "target" / D0WD_TARGET / "release" / D0WD_UART_BIN
    if bytes.fromhex(nsec_hex) not in binary.read_bytes():
        raise RuntimeError("binary verification failed (stale pin?): missing device nsec")
    return binary


# Attached-atom identities for radio quiesce (lab_keygen G*N; the smoke's
# GENERATOR_MUL is the superset — this map lists the L2CAP-capable atoms
# that can interfere on hci0).
QUIESCE_ATOM_MUL = {
    "81528A13B6": 11,
    "9D529068B4": 12,
}


def quiesce_peer_radios(repo: Path, exclude_serial: str) -> list[str]:
    """Flash every OTHER attached atom with the radio-silent UART variant.

    Two-atom interference (evidence 2026-09-03, hil-smoke atom-a leg): an
    atom running L2CAP firmware central-scans at boot and connects to any
    FIPS advert in range — including the TARGET atom's peripheral advert.
    The target's single BLE connection is then held by the peer (dialect
    tie-breaker deadlock, no exchange) and the lab daemon's probes time
    out. Quiescing peers to UART (no radio init) restores the one-advert
    topology every L2CAP scenario was validated under.

    Only touches serials in QUIESCE_ATOM_MUL that are physically attached;
    flashes happen under the caller's bench lock. Returns the serials
    quiesced."""
    quiesced: list[str] = []
    for serial, mul in QUIESCE_ATOM_MUL.items():
        if serial == exclude_serial:
            continue
        port = find_board(vidpid=D0WD_VIDPID, serial=serial)
        if port is None:
            continue
        binary = build_d0wd_uart(repo, lab_nsec(repo, mul))
        flash(port, binary, chip="esp32")
        time.sleep(2)  # boot fully off the radio before the target flashes
        quiesced.append(serial)
    return quiesced


def find_stm32_cdc() -> Path | None:
    """The STM32's USB CDC port (VID:PID c0de:cafe) — NOT the ST-Link."""
    for prefix in ("ttyACM", "ttyUSB"):
        for p in Path("/dev").glob(prefix + "*"):
            try:
                uevent = Path("/sys/class/tty", p.name, "device/../uevent").read_text()
            except OSError:
                continue
            m = re.search(r"^PRODUCT=(\S+)", uevent, re.M)
            if m and m.group(1).startswith(STM32_VIDPID):
                return p
    return None


def build_stm32(repo: Path, npub_hex: str) -> Path:
    """Host-toolchain build of the STM32 firmware with the peer npub pinned
    (the registry default targets the VPS — bench scenarios pin the lab
    daemon)."""
    subprocess.run(
        ["cargo", "build", "-p", "microfips", "--release",
         "--target", ARM_TARGET],
        cwd=repo, check=True, capture_output=True,
        env={**os.environ, "DEVICE_NPUB_HEX_vps": npub_hex},
    )
    elf = repo / "target" / ARM_TARGET / "release" / "microfips"
    bin_path = repo / "target" / ARM_TARGET / "release" / "microfips.bin"
    subprocess.run(
        ["arm-none-eabi-objcopy", "-O", "binary", str(elf), str(bin_path)],
        check=True, capture_output=True,
    )
    return bin_path


def flash_stm32(bin_path: Path) -> None:
    """st-flash (never probe-rs during USB testing — playbook/MCU lessons)."""
    require_board("stm32f469i-disco", "flash")
    subprocess.run(
        ["st-flash", "--connect-under-reset", "write", str(bin_path), "0x08000000"],
        check=True, capture_output=True, timeout=120,
    )


class SerialBridge:
    """tools/serial_udp_bridge.py as a managed subprocess (serial ↔ UDP)."""

    def __init__(self, repo: Path, serial_port: Path, bind_port: int,
                 udp_host: str, udp_port: int, log_file: Path):
        self.log_file = log_file
        self._proc = subprocess.Popen(
            [sys.executable, str(repo / "tools/serial_udp_bridge.py"),
             "--serial", str(serial_port), "--bind-port", str(bind_port),
             "--udp-host", udp_host, "--udp-port", str(udp_port)],
            stdout=log_file.open("wb"), stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )

    def read(self) -> str:
        try:
            return self.log_file.read_text(errors="replace")
        except OSError:
            return ""

    def wait_for(self, pattern: str, timeout: float = 45.0) -> float:
        import re as _re
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _re.search(pattern, self.read()):
                return timeout - (deadline - time.monotonic())
            time.sleep(1.0)
        raise TimeoutError(f"bridge: {pattern!r} not seen in {timeout}s")

    def stop(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()


def _serial_of(port: Path) -> str | None:
    props = subprocess.run(
        ["udevadm", "info", "-q", "property", str(port)],
        capture_output=True, text=True,
    ).stdout
    for line in props.splitlines():
        if line.startswith("ID_SERIAL_SHORT="):
            return line.split("=", 1)[1]
    return None


class ConsoleTap:
    """Detached no-reset console tap; read() returns decoded text so far.
    Real-UART adapters (FTDI atoms) REQUIRE `baud` and route through
    ftdi_tap.py (pyserial — the raw-termios tap drains the backlog then
    stops receiving live bytes on FTDI); USB-JTAG ports (S3) ignore baud
    and use raw_tap.py. Neither touches TIOCM, but opening an auto-reset
    board (M5 Atom) can still pulse DTR via the driver — a deterministic
    fresh boot with the tap attached (playbook pattern 3, amended).

    FIPS_LAB_TAP=ftdi|raw overrides the heuristic (tollgate-lab #4: keep
    the tap the single variable across runs of the same scenario; forcing
    ftdi without `baud` defaults to 115200)."""

    def __init__(self, port: Path, outfile: Path, baud: int | None = None):
        self.outfile = outfile
        impl = os.environ.get("FIPS_LAB_TAP", "auto").lower()
        if impl not in ("auto", "ftdi", "raw"):
            raise ValueError(f"FIPS_LAB_TAP must be auto|ftdi|raw, got {impl!r}")
        use_ftdi = (baud is not None) if impl == "auto" else (impl == "ftdi")
        if use_ftdi:
            argv = [sys.executable, str(FTDI_TAP_SCRIPT), str(port), str(outfile),
                    str(baud if baud is not None else 115200)]
        else:
            argv = [sys.executable, str(TAP_SCRIPT), str(port), str(outfile)]
        self._proc = subprocess.Popen(
            argv,
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

    def __init__(
        self,
        repo: Path,
        rekey_after_secs: int,
        workdir: Path,
        generator_mul: int = 8,
        port: int = 21213,
        ble: bool = False,
    ):
        """`generator_mul` selects the daemon identity (lab_keygen G*N) and
        `port` the UDP bind — a second instance with a different mul/port is
        the mDNS rogue-daemon pattern (audit #188 c2). `ble=True` adds the
        BLE L2CAP transport on hci0 alongside UDP (L2CAP bring-up scenario,
        #188 c4): fips config shape `transports.ble.adapter`."""
        self.repo = repo
        self.rekey_after_secs = rekey_after_secs
        self.workdir = workdir
        self.generator_mul = generator_mul
        self.port = port
        self.ble = ble
        workdir.mkdir(parents=True, exist_ok=True)
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
        nsec = lab_daemon_nsec(self.repo, self.generator_mul)
        cfg = template.replace("__LAB_DAEMON_NSEC__", nsec)
        if self.port != 21213:
            cfg = cfg.replace("192.168.13.221:21213", f"192.168.13.221:{self.port}")
        # Injection order matters: the rekey block must nest under `node:`,
        # so it goes FIRST — the control block then lands at top level
        # before `transports:` (both anchor on the same marker; reversing
        # the order silently nests rekey inside control and the daemon
        # falls back to the default cadence — found by the rekey soak).
        cfg = cfg.replace(
            "transports:",
            f"  rekey:\n    after_secs: {self.rekey_after_secs}\ntransports:",
        )
        cfg = cfg.replace(
            "transports:",
            f'control:\n  socket_path: "/tmp/bench-labd-{self.port}.sock"\ntransports:',
        )
        if self.ble:
            # Anchored on the udp block, not the "transports:" marker the
            # rekey/control injections rewrite above — order-independent.
            cfg = cfg.replace(
                "\n  udp:\n",
                "\n  ble:\n    adapter: hci0\n  udp:\n",
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

    def stop(self, restore: bool = True, graceful: bool = True) -> None:
        """Stop the scenario daemon. `restore=False` for mid-test restarts
        (link-death scenarios) so the standard daemon isn't bounced; the
        final teardown stop() restores it once. `graceful=False` kills with
        SIGKILL — no shutdown disconnect notifications, modeling gateway
        loss (the RX-silence path) instead of a clean goodbye."""
        if self._proc and self._proc.poll() is None:
            self._proc.terminate() if graceful else self._proc.kill()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if restore and self._standard_was_running:
            subprocess.run(
                ["bash", str(self.repo / "scripts/run_lab_daemon.sh")],
                capture_output=True, timeout=60,
            )


FIPS_NEXT_BIN = Path(os.environ.get(
    "FIPS_NEXT_BIN", "/tmp/opencode/fips-next/target/release/fips"
))


class BenchXxDaemon:
    """Upstream-next (XX/FMP-v1, 0.6.0-dev) bench daemon for the #193 wire —
    built from a jmcorgan/next worktree (AGENTS.md recipe; NEVER rebuilt in
    ~/src/fips, whose target/ is the system daemon). Consumes a prebuilt
    binary via FIPS_NEXT_BIN; skips cleanly when the worktree is gone
    (worktrees under /tmp are disposable by design). Config follows the
    next shape (node.rendezvous.lan.enabled, node.control.*) and the lab
    security checklist: deterministic G*N identity, interface-scoped bind,
    dedicated port (default 21214 — system 2121 and IK lab 21213 coexist).
    The node pins this daemon's npub, so the G*8 IK lab daemon's advert is
    rejected by the pin filter — no cross-dialect interference; this class
    never stops or restores the standard IK lab daemon."""

    def __init__(
        self,
        repo: Path,
        workdir: Path,
        generator_mul: int = 22,
        port: int = 21214,
        bind_ip: str = "192.168.13.221",
    ):
        self.repo = repo
        self.workdir = workdir
        self.generator_mul = generator_mul
        self.port = port
        self.bind_ip = bind_ip
        workdir.mkdir(parents=True, exist_ok=True)
        self.config = workdir / "xx-daemon.yaml"
        self.log = workdir / "xx-daemon.log"
        self.control_sock = f"/tmp/bench-xx-{port}.sock"
        self.fipsctl = FIPS_NEXT_BIN.with_name("fipsctl")
        self._proc = None

    def _pids_for(self) -> list[int]:
        out = subprocess.run(
            ["pgrep", "-f", f"fips --config {self.config}"], capture_output=True, text=True,
        ).stdout
        return [int(x) for x in out.split()]

    def available(self) -> str | None:
        if not FIPS_NEXT_BIN.exists():
            return (
                f"FIPS_NEXT_BIN missing ({FIPS_NEXT_BIN}) — rebuild the "
                "next-branch worktree per microfips AGENTS.md (#193 recipe)"
            )
        if not self.fipsctl.exists():
            return f"fipsctl missing next to {FIPS_NEXT_BIN}"
        return None

    def start(self) -> None:
        for pid in self._pids_for():
            subprocess.run(["kill", str(pid)], capture_output=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self._pids_for():
            time.sleep(0.5)

        nsec = lab_nsec(self.repo, self.generator_mul)
        self.config.write_text(
            "# Bench XX daemon (upstream next) — fips_lab.BenchXxDaemon\n"
            "node:\n"
            "  identity:\n"
            f"    nsec: {nsec}\n"
            "    persistent: false\n"
            "  log_level: debug\n"
            "  rendezvous:\n"
            "    lan:\n"
            "      enabled: true\n"
            "  control:\n"
            f'    socket_path: "{self.control_sock}"\n'
            "transports:\n"
            "  udp:\n"
            f'    bind_addr: "{self.bind_ip}:{self.port}"\n'
            "tun:\n"
            "  enabled: false\n"
            "dns:\n"
            "  enabled: false\n"
        )
        self.log.write_text("")
        self._proc = subprocess.Popen(
            [str(FIPS_NEXT_BIN), "--config", str(self.config)],
            stdout=self.log.open("ab"), stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            log = self.log.read_text(errors="replace")
            if "UDP transport started" in log:
                return
            if "Failed to start node" in log or self._proc.poll() is not None:
                raise RuntimeError(f"XX daemon failed: {log[-600:]}")
            time.sleep(0.5)
        raise TimeoutError(f"XX daemon transport not up: {self.log.read_text()[-600:]}")

    def log_text(self) -> str:
        return self.log.read_text(errors="replace")

    def peers(self) -> list[dict]:
        out = subprocess.run(
            [str(self.fipsctl), "-s", self.control_sock, "show", "peers"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            raise RuntimeError(f"fipsctl failed: {out.stderr[-300:]}")
        return json.loads(out.stdout).get("peers", [])

    def stop(self) -> None:
        for pid in self._pids_for():
            subprocess.run(["kill", str(pid)], capture_output=True)
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def make_run_dir(label: str) -> Path:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + label
    run_dir = RESULTS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def bench_available(board_serial: str, vidpid: str = S3_VIDPID) -> str | None:
    """Return a skip-reason if the bench can't run, else None."""
    if find_board(vidpid=vidpid, serial=board_serial) is None:
        return f"bench board {board_serial} not attached"
    if not (MICROFIPS_REPO / ".env").exists():
        return "microfips .env missing"
    if not FIPS_BIN.exists():
        return f"fips binary missing at {FIPS_BIN}"
    if not EXPORT_ESP.exists():
        return f"esp toolchain export missing at {EXPORT_ESP}"
    return None


def acquire_board_lock(name: str = "microfips-bench"):
    """Cross-project hardware lock (tollgate-lab). Prefer
    BoardReservation for board-level, cross-project coordination —
    this remains for session-scoped locking."""
    lock = HardwareLock(name)
    lock.acquire()
    return lock


def reserve_board(serial: str, project: str = "microfips", ttl_secs: int = 1800):
    """Cross-project board reservation (tollgate-lab reservation layer):
    visible to every Amperstrand project and every machine on the lab
    network sharing the reservation dir. Context manager."""
    return BoardReservation(serial, project, ttl_secs=ttl_secs)
