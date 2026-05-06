"""Noise keylog capture via FIPS_NOISE_KEYLOG env var."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FIPS_LINK_PREFIX = "FIPS_LINK"
FIPS_SESSION_PREFIX = "FIPS_SESSION"


@dataclass
class KeylogCapture:
    """Collect Noise keylog files from FIPS nodes after a test run.

    FIPS writes keylog entries when FIPS_NOISE_KEYLOG=<path> is set.
    Format: FIPS_LINK <local> <peer> <send_key> <recv_key>
            FIPS_SESSION <local> <peer> <send_key> <recv_key>

    This module does NOT restart FIPS with the env var — that requires
    the deploy/restart lifecycle. Instead it collects keylog files that
    were already written during the test.
    """

    results_dir: Path = Path(".")
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    enabled: bool = True

    def collect(self) -> dict[str, Any]:
        info: dict[str, Any] = {"enabled": self.enabled, "devices": {}}
        if not self.enabled:
            return info

        for alias, cfg in self.devices.items():
            dev_info = self._collect_device(alias, cfg)
            info["devices"][alias] = dev_info

        return info

    def _collect_device(self, alias: str, cfg: dict[str, Any]) -> dict[str, Any]:
        transport = cfg.get("transport", "local")
        keylog_path = cfg.get("keylog_path", "")

        dev_info: dict[str, Any] = {
            "transport": transport,
            "keylog_path": keylog_path,
            "file": None,
            "entries": 0,
            "link_keys": 0,
            "session_keys": 0,
        }

        if not keylog_path:
            return dev_info

        content = self._read_remote(keylog_path, cfg) if transport == "ssh" else self._read_local(keylog_path)
        if content is None:
            return dev_info

        link_count = sum(1 for line in content if line.startswith(FIPS_LINK_PREFIX))
        session_count = sum(1 for line in content if line.startswith(FIPS_SESSION_PREFIX))

        local_path = self.results_dir / f"keylog-{alias}.txt"
        local_path.write_text("\n".join(content) + "\n")
        dev_info["file"] = str(local_path)
        dev_info["entries"] = len(content)
        dev_info["link_keys"] = link_count
        dev_info["session_keys"] = session_count

        log.info("keylog %s: %d link + %d session keys → %s",
                 alias, link_count, session_count, local_path)
        return dev_info

    def _read_local(self, path: str) -> list[str] | None:
        try:
            return Path(path).read_text().strip().splitlines()
        except (FileNotFoundError, PermissionError):
            if not Path(path).exists():
                log.debug("keylog not found locally: %s", path)
                return None
            result = subprocess.run(
                ["sudo", "cat", path],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode != 0:
                log.debug("keylog read failed: %s", result.stderr.strip())
                return None
            return result.stdout.strip().splitlines()

    def _read_remote(self, path: str, cfg: dict[str, Any]) -> list[str] | None:
        host = cfg.get("host", "")
        user = cfg.get("user", "")
        target = f"{user}@{host}" if user else host
        use_sudo = cfg.get("sudo", False)
        cat_cmd = f"sudo cat {path}" if use_sudo else f"cat {path}"
        result = subprocess.run(
            ["ssh", target, cat_cmd],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode != 0:
            log.debug("keylog not found on %s: %s", target, result.stderr.strip())
            return None
        return result.stdout.strip().splitlines()
