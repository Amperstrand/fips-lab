"""BLE RSSI collector via hcitool on a remote Linux host."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ``hcitool rssi <addr>`` returns: "RSSI return value: -45"
_RSSI_RE = re.compile(r"RSSI return value:\s*(-?\d+)")


@dataclass
class RssiCollector:
    """Periodically sample BLE RSSI from a remote Linux host via SSH.

    Uses ``hcitool rssi <bdaddr>`` to read the current RSSI for a connected
    BLE device.  Runs collection in a background thread.

    Usage::

        collector = RssiCollector(
            device_alias="linux",
            host="192.168.1.218",
            user="pi",
            ble_addr="AA:BB:CC:DD:EE:FF",
            results_dir=run_dir,
        )
        collector.start()
        # ... run test ...
        collector.stop()
        summary = collector.summary()
    """

    device_alias: str
    host: str = ""
    user: str = ""
    ble_addr: str = ""
    interval_secs: float = 5.0
    results_dir: Path = Path(".")
    enabled: bool = True

    _samples: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _ssh_target: str = field(default="", init=False, repr=False)
    _consecutive_failures: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        target = self.host
        if self.user:
            target = f"{self.user}@{self.host}"
        self._ssh_target = target
        self._consecutive_failures = 0

    def _ssh(self, cmd: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["ssh", self._ssh_target, cmd],
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    def _sample_rssi(self) -> int | None:
        """Read a single RSSI sample via ``hcitool rssi <addr>``."""
        result = self._ssh(f"sudo hcitool rssi {self.ble_addr}")
        if result.returncode != 0:
            if self._consecutive_failures < 5:
                log.warning("hcitool rssi failed for %s: %s", self.ble_addr, result.stderr.strip())
            else:
                log.debug("hcitool rssi failed for %s: %s", self.ble_addr, result.stderr.strip())
            self._consecutive_failures += 1
            return None
        match = _RSSI_RE.search(result.stdout)
        if not match:
            log.debug("no RSSI match in output: %s", result.stdout.strip())
            self._consecutive_failures += 1
            return None
        self._consecutive_failures = 0
        return int(match.group(1))

    def _collection_loop(self) -> None:
        """Background thread: sample RSSI at regular intervals."""
        log.info(
            "RSSI collector started for %s (%s), interval=%.1fs",
            self.device_alias, self.ble_addr, self.interval_secs,
        )
        while not self._stop_event.is_set():
            rssi = self._sample_rssi()
            if rssi is not None:
                self._samples.append({"t": time.time(), "rssi": rssi})
            self._stop_event.wait(self.interval_secs)

        log.info(
            "RSSI collector stopped for %s: %d samples",
            self.device_alias, len(self._samples),
        )

    # -- Public API ----------------------------------------------------------

    def start(self) -> None:
        """Start background RSSI collection."""
        if not self.enabled or not self.host or not self.ble_addr:
            log.info("RSSI collection disabled for %s", self.device_alias)
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._collection_loop,
            name=f"rssi-{self.device_alias}",
            daemon=True,
        )
        self._thread.start()
        # Startup self-check: attempt one sample immediately
        time.sleep(0.5)
        rssi = self._sample_rssi()
        if rssi is None:
            log.warning(
                "RSSI collector for %s failed startup sample (consecutive failures: %d)",
                self.device_alias, self._consecutive_failures
            )

    def stop(self) -> dict[str, Any]:
        """Stop collection, save timeseries, return capture info dict."""
        info: dict[str, Any] = {
            "enabled": self.enabled,
            "device": self.device_alias,
            "host": self.host,
            "ble_addr": self.ble_addr,
            "samples": 0,
            "file": None,
        }
        if not self.enabled:
            return info

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_secs + 5)
            self._thread = None

        info["samples"] = len(self._samples)

        if self._samples:
            path = self.results_dir / f"rssi-timeseries-{self.device_alias}.json"
            path.write_text(
                json.dumps(self._samples, indent=2) + "\n",
                encoding="utf-8",
            )
            info["file"] = str(path)
            log.info("RSSI timeseries saved: %s (%d samples)", path, len(self._samples))

        return info

    def summary(self) -> dict[str, Any]:
        """Return summary stats {min, max, avg, samples, device, ble_addr}."""
        if not self._samples:
            return {
                "device": self.device_alias,
                "ble_addr": self.ble_addr,
                "samples": 0,
                "min": None,
                "max": None,
                "avg": None,
            }
        values = [s["rssi"] for s in self._samples]
        return {
            "device": self.device_alias,
            "ble_addr": self.ble_addr,
            "samples": len(values),
            "min": min(values),
            "max": max(values),
            "avg": round(sum(values) / len(values), 1),
        }
