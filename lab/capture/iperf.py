from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IperfSession:
    enabled: bool = False

    def run(self) -> dict:
        return {"enabled": self.enabled, "sessions": []}
