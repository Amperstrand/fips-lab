from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BtmonCapture:
    device_alias: str
    enabled: bool = False

    def start(self) -> None:
        # v0.1 placeholder: runner records intent, implementation starts after SSH transport hardens.
        return None

    def stop(self) -> None:
        return None
