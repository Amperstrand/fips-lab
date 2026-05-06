from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SerialLogCapture:
    device_alias: str
    enabled: bool = False

    def start(self) -> None:
        # v0.1 placeholder: pyserial streaming added after inventory is confirmed.
        return None

    def stop(self) -> None:
        return None
