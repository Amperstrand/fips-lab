from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

TIER_DURATIONS: dict[str, int] = {
    "smoke": 60,
    "short": 120,
    "medium": 300,
    "standard": 600,
    "extended": 1800,
    "marathon": 7200,
}

VALID_TIERS = set(TIER_DURATIONS.keys())


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
    def tier(self) -> str:
        scenario = self.raw.get("scenario") or {}
        return str(scenario.get("tier", "smoke"))

    @property
    def duration_secs(self) -> int:
        scenario = self.raw.get("scenario") or {}
        explicit = scenario.get("duration_secs")
        if explicit is not None:
            return int(explicit)
        return TIER_DURATIONS.get(self.tier, 60)

    @property
    def topology_devices(self) -> list[dict[str, Any]]:
        return list(((self.raw.get("topology") or {}).get("devices")) or [])

    @property
    def metrics(self) -> dict[str, Any]:
        return (((self.raw.get("actions") or {}).get("metrics")) or {})


def _validate(path: Path, raw: dict[str, Any]) -> None:
    if not isinstance(raw, dict):
        raise ValueError(f"scenario {path} must be a YAML mapping")
    scenario = raw.get("scenario") or {}
    tier = scenario.get("tier")
    if tier is not None and str(tier) not in VALID_TIERS:
        raise ValueError(
            f"scenario {path}: invalid tier '{tier}', "
            f"must be one of: {', '.join(sorted(TIER_DURATIONS.keys()))}"
        )
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
