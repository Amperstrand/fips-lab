from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Scenario:
    path: Path
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> "Scenario":
        resolved = Path(path).expanduser().resolve()
        with resolved.open() as handle:
            raw = yaml.safe_load(handle) or {}
        _validate(resolved, raw)
        return cls(path=resolved, raw=raw)

    @property
    def name(self) -> str:
        scenario = self.raw.get("scenario") or {}
        return str(scenario.get("name") or self.path.stem)

    @property
    def duration_secs(self) -> int:
        scenario = self.raw.get("scenario") or {}
        return int(scenario.get("duration_secs", 60))

    @property
    def topology_devices(self) -> list[dict[str, Any]]:
        return list(((self.raw.get("topology") or {}).get("devices")) or [])

    @property
    def metrics(self) -> dict[str, Any]:
        return (((self.raw.get("actions") or {}).get("metrics")) or {})


def _validate(path: Path, raw: dict[str, Any]) -> None:
    if not isinstance(raw, dict):
        raise ValueError(f"scenario {path} must be a YAML mapping")
    devices = ((raw.get("topology") or {}).get("devices")) or []
    if not devices:
        raise ValueError(f"scenario {path} must define topology.devices")
    seen = set()
    for entry in devices:
        if "id" not in entry or "inventory_ref" not in entry:
            raise ValueError("each topology device needs id and inventory_ref")
        if entry["id"] in seen:
            raise ValueError(f"duplicate topology device id: {entry['id']}")
        seen.add(entry["id"])
