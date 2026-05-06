"""Noise keylog capture via FIPS_NOISE_KEYLOG env var."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FIPS_LINK_PREFIX = "FIPS_LINK"
FIPS_SESSION_PREFIX = "FIPS_SESSION"

# Keylog line format: FIPS_LINK/FIPS_SESSION + 4 hex fields (64 hex chars each)
KEYLOG_LINE_RE = re.compile(
    r"^FIPS_(LINK|SESSION)\s+"
    r"([0-9a-f]{64})\s+"
    r"([0-9a-f]{64})\s+"
    r"([0-9a-f]{64})\s+"
    r"([0-9a-f]{64})$"
)

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


@dataclass
class KeylogEntry:
    kind: str  # "LINK" or "SESSION"
    local_npub_hex: str
    peer_npub_hex: str
    send_key_hex: str
    recv_key_hex: str


@dataclass
class KeylogParseResult:
    total_lines: int
    valid_entries: list[KeylogEntry]
    parse_errors: list[dict]  # [{line_number, raw_line, error}]
    peer_pairs: set[tuple[str, str]]  # canonicalized (sorted) hex npub pairs
    link_count: int
    session_count: int


def parse_keylog(path: str | Path) -> KeylogParseResult:
    """Parse a keylog file and validate format. Returns structured result."""
    lines = Path(path).read_text().strip().splitlines()

    valid_entries: list[KeylogEntry] = []
    parse_errors: list[dict] = []
    peer_pairs: set[tuple[str, str]] = set()
    link_count = 0
    session_count = 0

    for i, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        m = KEYLOG_LINE_RE.match(stripped)
        if not m:
            parse_errors.append({
                "line_number": i,
                "raw_line": stripped,
                "error": "line does not match FIPS_(LINK|SESSION) + 4×64-hex format",
            })
            continue

        kind = m.group(1)
        entry = KeylogEntry(
            kind=kind,
            local_npub_hex=m.group(2),
            peer_npub_hex=m.group(3),
            send_key_hex=m.group(4),
            recv_key_hex=m.group(5),
        )
        valid_entries.append(entry)

        if kind == "LINK":
            link_count += 1
        else:
            session_count += 1

        pair = tuple(sorted([entry.local_npub_hex, entry.peer_npub_hex]))
        peer_pairs.add(pair)  # type: ignore[arg-type]

    return KeylogParseResult(
        total_lines=len(lines),
        valid_entries=valid_entries,
        parse_errors=parse_errors,
        peer_pairs=peer_pairs,
        link_count=link_count,
        session_count=session_count,
    )


def _bech32_npub_to_hex(bech32_str: str) -> str | None:
    """Decode a bech32 npub string to lowercase hex (64 chars). Returns None on failure."""
    pos = bech32_str.rfind("1")
    if pos < 1 or pos + 7 > len(bech32_str):
        return None

    data_part = bech32_str[pos + 1:]
    values: list[int] = []
    for c in data_part:
        idx = _BECH32_CHARSET.find(c)
        if idx < 0:
            return None
        values.append(idx)

    # Exclude last 6 values (checksum), convert remaining 5-bit groups to 8-bit bytes
    data_5bit = values[:-6]
    acc = 0
    bits = 0
    result = bytearray()
    for v in data_5bit:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            result.append((acc >> bits) & 0xFF)

    return result.hex() or None


def verify_keylog_coverage(
    parse_results: dict[str, KeylogParseResult],
    peer_snapshot: dict,
) -> dict[str, dict]:
    """Cross-reference keylog peer pairs with connected peers from snapshots.

    Returns ``{alias: {connected_peers, covered_peers, coverage_pct, missing}}``.
    """
    coverage: dict[str, dict] = {}

    for alias, snap in peer_snapshot.items():
        show_peers = snap.get("show_peers", {})
        peers_list = show_peers.get("peers", [])

        connected_hex_npubs: set[str] = set()
        for p in peers_list:
            if p.get("connectivity") != "connected":
                continue
            npub_bech32 = p.get("npub", "")
            if not npub_bech32:
                continue
            hex_npub = _bech32_npub_to_hex(npub_bech32)
            if hex_npub:
                connected_hex_npubs.add(hex_npub)

        parsed = parse_results.get(alias)
        if parsed is None:
            coverage[alias] = {
                "connected_peers": len(connected_hex_npubs),
                "covered_peers": 0,
                "coverage_pct": 0.0,
                "missing": sorted(connected_hex_npubs),
            }
            continue

        keylog_npubs: set[str] = set()
        for entry in parsed.valid_entries:
            keylog_npubs.add(entry.local_npub_hex)
            keylog_npubs.add(entry.peer_npub_hex)

        covered = connected_hex_npubs & keylog_npubs
        missing = connected_hex_npubs - keylog_npubs
        total = len(connected_hex_npubs)
        pct = (len(covered) / total * 100.0) if total > 0 else 100.0

        coverage[alias] = {
            "connected_peers": total,
            "covered_peers": len(covered),
            "coverage_pct": round(pct, 1),
            "missing": sorted(missing),
        }

    return coverage


# ---------------------------------------------------------------------------
# KeylogCapture
# ---------------------------------------------------------------------------

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

        try:
            parsed = parse_keylog(local_path)
            dev_info["parse_errors"] = len(parsed.parse_errors)
            if parsed.parse_errors:
                log.warning("keylog %s: %d parse errors", alias, len(parsed.parse_errors))
        except Exception:
            log.debug("keylog %s: parse validation skipped", alias)

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
