from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .device import Device

log = logging.getLogger(__name__)

_READINESS_TIMEOUT_SECS = 60
_READINESS_POLL_INTERVAL_SECS = 2

# Pattern to extract commit from fips version string like "0.3.0-dev (rev c52b2b71c5)"
_VERSION_COMMIT_RE = re.compile(r"rev ([0-9a-f]{7,40})", re.IGNORECASE)


class DeployManager:
    """Manages FIPS deploy/restart lifecycle for test devices."""

    def __init__(
        self,
        devices: dict[str, Device],
        configs: dict[str, dict],
        results_dir: Path,
    ):
        self._devices = devices
        self._configs = configs
        self._results_dir = results_dir

    def restart_all(
        self, keylog: bool = True, expected_commit: str | None = None
    ) -> None:
        targets = self._fips_devices()
        if not targets:
            log.warning("No FIPS devices to restart")
            return

        self.stop_all()

        if keylog:
            self._clear_keylogs(targets)

        self._start_all(targets, keylog)
        self._poll_all(targets)

        if expected_commit:
            self._verify_binary_versions(targets, expected_commit)

    def stop_all(self) -> None:
        """Stop all FIPS nodes. Best-effort — log errors but don't raise."""
        targets = self._fips_devices()
        for alias, (device, cfg) in targets.items():
            transport = cfg.get("transport", "local")
            if transport == "local":
                kill_cmd: list[str] = ["pkill", "-9", "fips"]
            else:
                kill_cmd = ["killall", "-9", "fips"]
            result = device.run(kill_cmd)
            if result.returncode != 0:
                log.debug(
                    "Stop %s: exit %d (%s) — may not have been running",
                    alias,
                    result.returncode,
                    result.stderr.strip(),
                )
            else:
                log.info("Stopped FIPS on %s", alias)
            socket_path = cfg.get("control_socket")
            if socket_path:
                device.run(["rm", "-f", str(socket_path)])

    def _fips_devices(self) -> dict[str, tuple[Device, dict[str, Any]]]:
        out: dict[str, tuple[Device, dict[str, Any]]] = {}
        for alias, device in self._devices.items():
            cfg = self._configs.get(alias, {})
            if cfg.get("fips_binary") and cfg.get("type") == "fips":
                out[alias] = (device, cfg)
        return out

    def _clear_keylogs(self, targets: dict[str, tuple[Device, dict[str, Any]]]) -> None:
        for alias, (device, cfg) in targets.items():
            keylog_path = cfg.get("keylog_path")
            if not keylog_path:
                continue
            result = device.run(["rm", "-f", str(keylog_path)])
            if result.returncode != 0:
                log.warning(
                    "Failed to delete keylog %s on %s: %s",
                    keylog_path,
                    alias,
                    result.stderr.strip(),
                )

    def _start_all(
        self,
        targets: dict[str, tuple[Device, dict[str, Any]]],
        keylog: bool,
    ) -> None:
        for alias, (device, cfg) in targets.items():
            transport = cfg.get("transport", "local")
            fips_binary = str(cfg["fips_binary"])
            config_path = str(cfg["config_path"])
            keylog_path = cfg.get("keylog_path")
            env_keylog = str(keylog_path) if keylog and keylog_path else ""

            if transport == "local":
                self._start_local(alias, cfg, fips_binary, config_path, env_keylog)
            elif transport == "ssh":
                self._start_ssh(alias, cfg, fips_binary, config_path, env_keylog)
            else:
                log.warning("Skipping start for %s: unsupported transport %s", alias, transport)

    def _start_local(
        self,
        alias: str,
        cfg: dict[str, Any],
        fips_binary: str,
        config_path: str,
        keylog_path: str,
    ) -> None:
        # caffeinate -i required for macOS CoreBluetooth (issue #99)
        env = dict(os.environ)
        if keylog_path:
            env["FIPS_NOISE_KEYLOG"] = keylog_path
        use_sudo = cfg.get("sudo", False)
        if use_sudo:
            cmd = ["sudo"]
            if keylog_path:
                cmd.append(f"FIPS_NOISE_KEYLOG={keylog_path}")
            cmd.extend(["caffeinate", "-i", fips_binary, "--config", config_path])
        else:
            cmd = ["caffeinate", "-i", fips_binary, "--config", config_path]
        log.info("Starting FIPS on %s: %s", alias, " ".join(cmd))
        try:
            subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise RuntimeError(f"Failed to start FIPS on {alias}: {exc}") from exc

    def _start_ssh(
        self,
        alias: str,
        cfg: dict[str, Any],
        fips_binary: str,
        config_path: str,
        keylog_path: str,
    ) -> None:
        host = cfg.get("host") or cfg.get("ssh_host")
        user = cfg.get("user") or cfg.get("ssh_user")
        target = f"{user}@{host}" if user else str(host)

        keylog_env = f"FIPS_NOISE_KEYLOG={keylog_path} " if keylog_path else ""
        sudo = "sudo " if cfg.get("sudo", False) else ""
        remote_cmd = (
            f"{sudo}{keylog_env}"
            f"nohup {fips_binary} --config {config_path} "
            f"> /dev/null 2>&1 &"
        )
        ssh_argv = ["ssh", target, remote_cmd]
        log.info("Starting FIPS on %s: ssh %s '...'", alias, target)
        try:
            subprocess.run(ssh_argv, capture_output=True, text=True, timeout=15, check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(f"Failed to start FIPS on {alias}: {exc}") from exc

    def _poll_all(self, targets: dict[str, tuple[Device, dict[str, Any]]]) -> None:
        for alias, (device, _cfg) in targets.items():
            self._poll_ready(alias, device)

    def _poll_ready(self, alias: str, device: Device) -> None:
        log.info("Polling readiness for %s (timeout %ds)", alias, _READINESS_TIMEOUT_SECS)
        deadline = time.time() + _READINESS_TIMEOUT_SECS
        while time.time() < deadline:
            result = device.query("show_status")
            if result and not result.get("error"):
                if "status" in result or "uptime_secs" in result:
                    log.info("Device %s ready", alias)
                    return
            remaining = deadline - time.time()
            if remaining > _READINESS_POLL_INTERVAL_SECS:
                time.sleep(_READINESS_POLL_INTERVAL_SECS)
            else:
                break
        raise RuntimeError(
            f"Device {alias} failed to start within {_READINESS_TIMEOUT_SECS}s"
        )

    def _verify_binary_versions(
        self,
        targets: dict[str, tuple[Device, dict[str, Any]]],
        expected_commit: str,
    ) -> None:
        expected_short = expected_commit[:7].lower()
        mismatches: list[str] = []

        for alias, (device, _cfg) in targets.items():
            status = device.query("show_status")
            if not status:
                log.warning("%s: no status response for version check", alias)
                continue
            version_str = status.get("version", "")
            match = _VERSION_COMMIT_RE.search(version_str)
            if not match:
                log.warning("%s: cannot parse version '%s'", alias, version_str)
                continue
            running_commit = match.group(1).lower()
            if running_commit.startswith(expected_short) or expected_short.startswith(running_commit):
                log.info("%s: binary version OK (rev %s)", alias, running_commit)
            else:
                msg = (
                    f"{alias}: binary commit {running_commit} != "
                    f"expected {expected_short} (from git HEAD)"
                )
                log.error(msg)
                mismatches.append(msg)

        if mismatches:
            raise RuntimeError(
                "Binary version mismatch! One or more devices are running "
                "stale binaries. Rebuild (cargo build --release) and retry.\n"
                + "\n".join(mismatches)
            )
