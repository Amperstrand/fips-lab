from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Inventory:
    path: Path
    lab: dict[str, Any]
    devices: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: str | Path) -> "Inventory":
        resolved = Path(path).expanduser().resolve()
        with resolved.open() as handle:
            raw = yaml.safe_load(handle) or {}
        devices = raw.get("devices") or {}
        if not isinstance(devices, dict) or not devices:
            raise ValueError(f"inventory {resolved} must define at least one device")
        return cls(path=resolved, lab=raw.get("lab") or {}, devices=devices)

    def device(self, name: str) -> dict[str, Any]:
        try:
            return self.devices[name]
        except KeyError as exc:
            raise KeyError(f"unknown inventory device: {name}") from exc
