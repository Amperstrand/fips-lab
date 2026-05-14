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
    """Manages FIPS deploy/restart and microfips flashing lifecycle."""

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
        self._reset_esp32_devices()

        if keylog:
            self._clear_keylogs(targets)

        self._clear_event_logs(targets)
        self._start_all(targets, keylog, event_log=True)
        self._poll_all(targets)

        if expected_commit:
            self._verify_binary_versions(targets, expected_commit)

    def flash_all_microfips(self) -> None:
        """Flash firmware to all microfips devices defined in scenario topology."""
        targets = self._microfips_devices()
        if not targets:
            log.info("No microfips devices to flash")
            return
        for alias, (device, cfg) in targets.items():
            self._flash_one(alias, device, cfg)

    def stop_all(self) -> None:
        """Stop all FIPS nodes. Best-effort — log errors but don't raise."""
        targets = self._fips_devices()
        for alias, (device, cfg) in targets.items():
            transport = cfg.get("transport", "local")
            if transport == "local":
                kill_cmd: list[str] = ["pkill", "-9", "fips"]
            else:
                svc = cfg.get("service")
                if isinstance(svc, dict) and svc.get("kind") == "systemd":
                    device.run(["sudo", "systemctl", "stop", svc.get("name", "fips")])
                    device.run(["sudo", "systemctl", "unset-environment",
                                "FIPS_NOISE_KEYLOG", "FIPS_BLE_EVENT_LOG"])
                kill_cmd = ["sudo", "killall", "-9", "fips"]
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

    def _clear_event_logs(self, targets: dict[str, tuple[Device, dict[str, Any]]]) -> None:
        for alias, (device, cfg) in targets.items():
            event_log_path = self._event_log_path(alias, cfg)
            if not event_log_path:
                continue
            result = device.run(["rm", "-f", str(event_log_path)])
            if result.returncode != 0:
                log.debug("Failed to delete event log %s on %s: %s",
                          event_log_path, alias, result.stderr.strip())

    @staticmethod
    def _event_log_path(alias: str, cfg: dict[str, Any]) -> str | None:
        """Derive the BLE event log path for a device.

        Uses the keylog_path directory with a different filename so event logs
        land alongside keylogs (e.g. /tmp/fips-ble-events-mac.jsonl).
        """
        keylog_path = cfg.get("keylog_path")
        if not keylog_path:
            return None
        p = Path(keylog_path)
        return str(p.parent / f"fips-ble-events-{alias}.jsonl")

    def _start_all(
        self,
        targets: dict[str, tuple[Device, dict[str, Any]]],
        keylog: bool,
        event_log: bool = False,
    ) -> None:
        for alias, (device, cfg) in targets.items():
            transport = cfg.get("transport", "local")
            fips_binary = str(cfg["fips_binary"])
            config_path = str(cfg["config_path"])
            keylog_path = cfg.get("keylog_path")
            env_keylog = str(keylog_path) if keylog and keylog_path else ""
            env_event_log = (
                self._event_log_path(alias, cfg) if event_log and keylog_path else ""
            )

            if transport == "local":
                self._start_local(alias, cfg, fips_binary, config_path, env_keylog, env_event_log)
            elif transport == "ssh":
                self._start_ssh(alias, cfg, fips_binary, config_path, env_keylog, env_event_log)
            else:
                log.warning("Skipping start for %s: unsupported transport %s", alias, transport)

    def _start_local(
        self,
        alias: str,
        cfg: dict[str, Any],
        fips_binary: str,
        config_path: str,
        keylog_path: str,
        event_log_path: str = "",
    ) -> None:
        env = dict(os.environ)
        if keylog_path:
            env["FIPS_NOISE_KEYLOG"] = keylog_path
        if event_log_path:
            env["FIPS_BLE_EVENT_LOG"] = event_log_path
        use_sudo = cfg.get("sudo", False)
        if use_sudo:
            cmd = ["sudo"]
            if keylog_path:
                cmd.append(f"FIPS_NOISE_KEYLOG={keylog_path}")
            if event_log_path:
                cmd.append(f"FIPS_BLE_EVENT_LOG={event_log_path}")
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
        event_log_path: str = "",
    ) -> None:
        host = cfg.get("host") or cfg.get("ssh_host")
        user = cfg.get("user") or cfg.get("ssh_user")
        target = f"{user}@{host}" if user else str(host)

        # Reset BLE adapter to clear stale discovery sessions.
        # BlueZ keeps discovery active across process restarts, which prevents
        # FIPS from setting its own discovery filter and scanning for peers.
        ble_adapter = cfg.get("ble_adapter", "hci0")
        reset_cmd = f"sudo hciconfig {ble_adapter} down && sleep 1 && sudo hciconfig {ble_adapter} up"
        log.info("Resetting BLE adapter %s on %s", ble_adapter, alias)
        try:
            subprocess.run(
                ["ssh", target, reset_cmd],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.warning("BLE adapter reset failed on %s: %s", alias, exc)

        svc = cfg.get("service")
        if isinstance(svc, dict) and svc.get("kind") == "systemd" and svc.get("start", True):
            self._start_ssh_systemd(alias, target, svc, keylog_path, event_log_path)
        else:
            self._start_ssh_nohup(alias, cfg, target, fips_binary, config_path, keylog_path, event_log_path)

    def _start_ssh_systemd(
        self,
        alias: str,
        target: str,
        svc: dict[str, Any],
        keylog_path: str,
        event_log_path: str,
    ) -> None:
        svc_name = svc.get("name", "fips")
        env_vars: list[str] = []
        if keylog_path:
            env_vars.append(f"FIPS_NOISE_KEYLOG={keylog_path}")
        if event_log_path:
            env_vars.append(f"FIPS_BLE_EVENT_LOG={event_log_path}")

        remote_cmds: list[str] = []
        if env_vars:
            remote_cmds.append(
                f"sudo systemctl set-environment {' '.join(env_vars)}"
            )
        remote_cmds.append(f"sudo systemctl start {svc_name}")
        remote_cmd = " && ".join(remote_cmds)
        log.info("Starting FIPS on %s via systemd service %s", alias, svc_name)
        try:
            subprocess.run(
                ["ssh", target, remote_cmd],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(f"Failed to start FIPS on {alias} via systemd: {exc}") from exc

    @staticmethod
    def _start_ssh_nohup(
        alias: str,
        cfg: dict[str, Any],
        target: str,
        fips_binary: str,
        config_path: str,
        keylog_path: str,
        event_log_path: str,
    ) -> None:
        keylog_env = f"FIPS_NOISE_KEYLOG={keylog_path} " if keylog_path else ""
        event_env = f"FIPS_BLE_EVENT_LOG={event_log_path} " if event_log_path else ""
        sudo = "sudo " if cfg.get("sudo", False) else ""
        remote_cmd = (
            f"{sudo}{keylog_env}{event_env}"
            f"nohup {fips_binary} --config {config_path} "
            f"> /dev/null 2>&1 &"
        )
        log.info("Starting FIPS on %s: ssh %s '...'", alias, target)
        try:
            subprocess.run(
                ["ssh", target, remote_cmd],
                capture_output=True, text=True, timeout=15, check=False,
            )
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

    def _reset_esp32_devices(self) -> None:
        """Reset all ESP32/microfips devices to clear their RAM state.
        
        ESP32 stores backoff state in RAM (StaticCell), which persists across
        fips-lab restarts. Resetting clears this state to avoid INSUFFICIENT_DATA
        verdicts from stale deny states.
        """
        targets = self._microfips_devices()
        if not targets:
            log.info("No ESP32 devices to reset")
            return

        for alias, (device, cfg) in targets.items():
            transport = cfg.get("transport", "serial")
            if transport == "serial":
                # Local ESP32 - no reset needed (device isn't running)
                continue

            host = cfg.get("host") or cfg.get("ssh_host", "")
            user = cfg.get("user") or cfg.get("ssh_user", "")
            serial_port = cfg.get("serial_port", "")

            if not host or not serial_port:
                log.warning("ESP32 %s: missing host or serial_port, skipping reset", alias)
                continue

            target = f"{user}@{host}" if user else host
            reset_cmd = (
                f"python3 -c \"import serial, time; "
                f"s = serial.Serial('{serial_port}', 115200); "
                f"s.dtr = False; s.rts = True; "
                f"time.sleep(0.1); s.rts = False; "
                f"time.sleep(0.5); s.close()\""
            )

            log.info("Resetting ESP32 %s via SSH", alias)
            try:
                result = subprocess.run(
                    ["ssh", target, reset_cmd],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if result.returncode == 0:
                    log.info("ESP32 %s reset successful", alias)
                else:
                    log.warning(
                        "ESP32 %s reset failed (exit %d): %s",
                        alias,
                        result.returncode,
                        result.stderr.strip(),
                    )
            except (subprocess.TimeoutExpired, OSError) as exc:
                log.warning("ESP32 %s reset timeout or error: %s", alias, exc)

    def _microfips_devices(self) -> dict[str, tuple[Device, dict[str, Any]]]:
        out: dict[str, tuple[Device, dict[str, Any]]] = {}
        for alias, device in self._devices.items():
            cfg = self._configs.get(alias, {})
            if cfg.get("type") == "microfips" and cfg.get("firmware", {}).get("flash"):
                out[alias] = (device, cfg)
        return out

    def _flash_one(self, alias: str, device: Device, cfg: dict[str, Any]) -> None:
        firmware_cfg = cfg.get("firmware", {})
        flash_cfg = firmware_cfg.get("flash", {})
        artifact = firmware_cfg.get("artifact", "")
        tool = flash_cfg.get("tool", "esptool.py")
        chip = flash_cfg.get("chip", "esp32")
        baud = flash_cfg.get("baud", 921600)
        address = flash_cfg.get("address", "0x0")
        transport = cfg.get("transport", "serial")
        serial_port = cfg.get("serial_port", "")

        if not artifact:
            raise RuntimeError(f"microfips {alias}: no firmware.artifact configured")

        if transport == "serial":
            self._flash_local(alias, artifact, tool, chip, baud, address, serial_port)
        elif transport == "serial-via-ssh":
            host = cfg.get("host") or cfg.get("ssh_host", "")
            user = cfg.get("user") or cfg.get("ssh_user", "")
            self._flash_via_ssh(alias, artifact, tool, chip, baud, address, serial_port, host, user)
        else:
            log.warning("Skipping flash for %s: unsupported transport %s", alias, transport)

    def _flash_local(
        self,
        alias: str,
        artifact: str,
        tool: str,
        chip: str,
        baud: int,
        address: str,
        serial_port: str,
    ) -> None:
        argv = [
            tool, "--chip", chip, "--port", serial_port,
            "--baud", str(baud), "write_flash", address, artifact,
        ]
        log.info("Flashing %s: %s", alias, " ".join(argv))
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
        except FileNotFoundError:
            raise RuntimeError(f"microfips {alias}: flash tool '{tool}' not found (install esptool)")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"microfips {alias}: flash timed out after 120s")

        if result.returncode != 0:
            raise RuntimeError(
                f"microfips {alias}: flash failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        log.info("Flashed %s successfully", alias)

        if self._results_dir:
            flash_log = self._results_dir / f"flash-{alias}.log"
            flash_log.write_text(f"$ {' '.join(argv)}\n{result.stdout}\n{result.stderr}\n")

    def _flash_via_ssh(
        self,
        alias: str,
        artifact: str,
        tool: str,
        chip: str,
        baud: int,
        address: str,
        serial_port: str,
        host: str,
        user: str,
    ) -> None:
        target = f"{user}@{host}" if user else host
        remote_artifact = f"/tmp/fips-lab-flash-{alias}.bin"

        log.info("Copying firmware to %s:%s", target, remote_artifact)
        scp_result = subprocess.run(
            ["scp", artifact, f"{target}:{remote_artifact}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if scp_result.returncode != 0:
            raise RuntimeError(f"microfips {alias}: scp failed: {scp_result.stderr.strip()}")

        remote_cmd = (
            f"{tool} --chip {chip} --port {serial_port} "
            f"--baud {baud} write_flash {address} {remote_artifact} "
            f"&& rm -f {remote_artifact}"
        )
        log.info("Flashing %s via SSH: %s", alias, remote_cmd)
        ssh_result = subprocess.run(
            ["ssh", target, remote_cmd],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if ssh_result.returncode != 0:
            raise RuntimeError(
                f"microfips {alias}: remote flash failed: {ssh_result.stderr.strip()}"
            )
        log.info("Flashed %s successfully via SSH", alias)

    def run_stm32_hil(self) -> dict[str, Any]:
        """Run HIL tests on all stm32-hil devices. Returns per-device results."""
        targets = {
            alias: (device, cfg)
            for alias, device in self._devices.items()
            if self._configs.get(alias, {}).get("type") == "stm32-hil"
        }
        if not targets:
            return {}

        results: dict[str, Any] = {}
        for alias, (device, cfg) in targets.items():
            hil_script = cfg.get("hil_script", "run_hil.sh")
            hil_args = cfg.get("hil_args", "--json")
            repo_path = cfg.get("repo_path", "")
            cwd = repo_path if repo_path and Path(repo_path).is_dir() else None

            argv = ["bash", hil_script] + hil_args.split()
            log.info("Running STM32 HIL on %s: %s", alias, " ".join(argv))

            if self._results_dir:
                log_path = self._results_dir / f"stm32-hil-{alias}.log"
            else:
                log_path = None

            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    check=False,
                    cwd=cwd,
                )
            except FileNotFoundError:
                raise RuntimeError(f"stm32-hil {alias}: script '{hil_script}' not found")
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"stm32-hil {alias}: HIL runner timed out after 600s")

            result: dict[str, Any] = {
                "exit_code": proc.returncode,
                "passed": proc.returncode == 0,
            }

            if proc.stdout:
                result["stdout"] = proc.stdout
                try:
                    import json as _json
                    result["json"] = _json.loads(proc.stdout)
                except (ValueError, _json.JSONDecodeError):
                    pass

            if proc.stderr:
                result["stderr"] = proc.stderr

            if log_path:
                log_path.write_text(
                    f"$ {' '.join(argv)}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
                )

            results[alias] = result
            status = "PASSED" if result["passed"] else "FAILED"
            log.info("STM32 HIL %s: %s (exit %d)", alias, status, proc.returncode)

        return results
