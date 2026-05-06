from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def lab_acl_entries(resolved_devices: dict[str, dict[str, Any]], include_aliases: list[str]) -> tuple[list[str], list[str]]:
    allow: list[str] = []
    missing: list[str] = []
    for alias in include_aliases:
        device = resolved_devices.get(alias)
        if not device:
            missing.append(f"{alias}: not in topology")
            continue
        identity = device.get("identity") or {}
        npub = identity.get("npub")
        if npub:
            allow.append(str(npub))
        else:
            missing.append(f"{alias}: missing identity.npub")
    return allow, missing


def write_lab_acl(output_dir: Path, resolved_devices: dict[str, dict[str, Any]], isolation: dict[str, Any]) -> dict[str, Any]:
    include = list(isolation.get("include_devices") or resolved_devices.keys())
    allow, missing = lab_acl_entries(resolved_devices, include)
    acl_dir = output_dir / "generated-acl"
    acl_dir.mkdir(parents=True, exist_ok=True)
    allow_path = acl_dir / "peers.allow"
    deny_path = acl_dir / "peers.deny"
    allow_path.write_text("".join(f"{entry}\n" for entry in allow))
    deny_path.write_text("ALL\n" if isolation.get("write_peers_deny_all", True) else "")
    return {
        "mode": isolation.get("mode", "lab-allowlist"),
        "peers_allow": str(allow_path),
        "peers_deny": str(deny_path),
        "allow_entries": allow,
        "missing_identities": missing,
        "note": "Copy peers.allow and peers.deny to /etc/fips on FIPS nodes before restart.",
    }


def write_resolved_devices(output_dir: Path, devices: dict[str, dict[str, Any]]) -> None:
    path = output_dir / "devices.yaml"
    path.write_text(yaml.safe_dump({"devices": devices}, sort_keys=False))
