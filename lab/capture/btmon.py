"""BLE HCI capture via btmon on a remote Linux host."""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class BtmonCapture:
    """Start/stop btmon on a Linux host and fetch the btsnoop file.

    Uses SSH to run ``sudo btmon -w <path>`` in the background, then
    ``sudo kill`` to stop it and ``scp`` to copy the file locally.
    """

    device_alias: str
    host: str = ""
    user: str = ""
    adapter: str = "hci0"
    remote_dir: str = "/tmp/fips-lab-capture"
    results_dir: Path = Path(".")
    enabled: bool = True
    _remote_path: str = field(default="", init=False, repr=False)
    _pid: str = field(default="", init=False, repr=False)
    _ssh_target: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        target = self.host
        if self.user:
            target = f"{self.user}@{self.host}"
        self._ssh_target = target
        self._remote_path = f"{self.remote_dir}/btmon-{int(time.time())}.btsnoop"

    def _ssh(self, cmd: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["ssh", self._ssh_target, cmd],
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    def start(self) -> None:
        if not self.enabled or not self.host:
            log.info("btmon capture disabled for %s", self.device_alias)
            return

        self._ssh(f"mkdir -p {self.remote_dir}")

        # Start btmon in background, writing btsnoop format
        result = self._ssh(
            f"sudo nohup btmon -i {self.adapter} -w {self._remote_path} "
            f"> /dev/null 2>&1 & echo $!"
        )
        self._pid = result.stdout.strip().split("\n")[-1].strip()

        if not self._pid:
            log.warning("btmon failed to start on %s", self.host)
            return

        # Give btmon a moment to open the adapter
        time.sleep(1)

        # Verify process is alive
        check = self._ssh(f"kill -0 {self._pid} 2>/dev/null && echo alive")
        if "alive" not in check.stdout:
            log.warning("btmon PID %s not alive on %s", self._pid, self.host)
            self._pid = ""
            return

        log.info("btmon capture started on %s (pid=%s, adapter=%s)",
                 self.host, self._pid, self.adapter)

    def stop(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "enabled": self.enabled,
            "device": self.device_alias,
            "host": self.host,
            "adapter": self.adapter,
            "file": None,
            "size_bytes": 0,
        }

        if not self.enabled or not self._pid:
            return info

        # Stop btmon gracefully (SIGTERM), then SIGKILL if needed
        self._ssh(f"sudo kill {self._pid} 2>/dev/null")
        time.sleep(1)
        self._ssh(f"sudo kill -9 {self._pid} 2>/dev/null")

        # Check remote file exists and get size
        stat = self._ssh(f"stat -c '%s' {self._remote_path} 2>/dev/null")
        size_str = stat.stdout.strip().strip("'")
        if not size_str or not size_str.isdigit():
            log.warning("btmon capture file not found on remote: %s", self._remote_path)
            return info

        remote_size = int(size_str)
        info["size_bytes"] = remote_size

        # Copy btsnoop file locally
        local_path = self.results_dir / "btmon.btsnoop"
        scp_result = subprocess.run(
            ["scp", f"{self._ssh_target}:{self._remote_path}", str(local_path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if scp_result.returncode == 0:
            info["file"] = str(local_path)
            log.info("btmon capture saved: %s (%d bytes)", local_path, remote_size)
        else:
            log.warning("scp failed for btmon capture: %s", scp_result.stderr.strip())

        # Cleanup remote
        self._ssh(f"rm -f {self._remote_path}")

        return info
