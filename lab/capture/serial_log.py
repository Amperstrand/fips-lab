"""Serial log streaming for microcontrollers."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class SerialLogCapture:
    """Stream serial output from a device to a local file.

    For locally attached devices, uses ``pyserial`` directly.
    For serial-via-ssh devices, runs ``cat /dev/ttyUSBx`` over SSH.
    """

    device_alias: str
    transport: str = "serial"
    host: str = ""
    user: str = ""
    serial_port: str = ""
    baud_rate: int = 115200
    results_dir: Path = Path(".")
    enabled: bool = True
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop_flag: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lines: list[str] = field(default_factory=list, init=False, repr=False)

    @property
    def _local_path(self) -> Path:
        return self.results_dir / f"serial-{self.device_alias}.log"

    def _ssh_target(self) -> str:
        if self.user:
            return f"{self.user}@{self.host}"
        return self.host

    def _stream_via_ssh(self) -> None:
        target = self._ssh_target()
        proc = subprocess.Popen(
            ["ssh", target, f"sudo cat {self.serial_port}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        assert proc.stdout is not None
        with self._local_path.open("w") as fh:
            while not self._stop_flag.is_set():
                line = proc.stdout.readline()
                if not line:
                    break
                ts = time.strftime("%H:%M:%S")
                tagged = f"[{ts}] {line}"
                fh.write(tagged)
                self._lines.append(tagged)
        proc.terminate()
        proc.wait(timeout=5)

    def start(self) -> None:
        if not self.enabled or not self.serial_port:
            log.info("serial capture disabled for %s", self.device_alias)
            return

        if self.transport == "serial-via-ssh" and self.host:
            self._stop_flag.clear()
            self._thread = threading.Thread(target=self._stream_via_ssh, daemon=True)
            self._thread.start()
            log.info("serial capture started for %s via SSH (%s)", self.device_alias, self.serial_port)
        else:
            log.info("serial capture for local devices not yet implemented (%s)", self.device_alias)

    def stop(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "enabled": self.enabled,
            "device": self.device_alias,
            "port": self.serial_port,
            "file": None,
            "lines": 0,
        }
        if not self.enabled:
            return info

        self._stop_flag.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        local = self._local_path
        if local.exists():
            info["file"] = str(local)
            info["lines"] = len(self._lines)
            log.info("serial capture saved: %s (%d lines)", local, len(self._lines))

        return info
