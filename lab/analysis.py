"""Post-test analysis and reporting module for fips-lab."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PeerMetrics:
    pair: str
    loss_min: float
    loss_avg: float
    loss_max: float
    rtt_min: float | None
    rtt_avg: float | None
    rtt_max: float | None
    samples: int


@dataclass
class KeyExchangeInfo:
    pair: str
    link_keys: int
    session_keys: int
    coverage: str


@dataclass
class RekeyStats:
    pair: str
    total_rekeys: int  # link_keys - 1 (initial handshake doesn't count as rekey)
    rekey_interval_avg_secs: float | None  # duration_secs / max(1, total_rekeys)
    rekeys_per_hour: float | None  # (total_rekeys / duration_secs) * 3600


@dataclass
class DisconnectEvent:
    pair: str
    t: int  # timestamp in secs when disconnect detected
    reconnected: bool  # did the pair come back before test end?


@dataclass
class KeylogVerification:
    pair: str
    local_keys_parsed: int  # from parse_keylog for this device
    parse_errors: int  # malformed lines
    unique_peer_pairs: int  # canonicalized peer pairs found
    decryption_ready: bool  # True if we have keys for both directions
    note: str  # e.g. "Ready for Wireshark decryption (needs fips-dissector.lua)"


@dataclass
class AssertionResult:
    name: str
    expected: str
    actual: str
    passed: bool


@dataclass
class AnalysisReport:
    scenario_name: str
    timestamp: str
    duration_secs: int
    verdict: str
    connections: list[dict]
    peer_metrics: list[PeerMetrics]
    key_exchange: list[KeyExchangeInfo]
    captures: dict
    rekey_stats: list[RekeyStats]
    disconnects: list[DisconnectEvent]
    keylog_verification: list[KeylogVerification]
    assertions: list[AssertionResult]
    memory: dict


# ---------------------------------------------------------------------------
# Helpers – file I/O
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Any | None:
    """Load a JSON file, returning None gracefully on missing / corrupt."""
    if not path.exists():
        logger.warning("Missing file: %s", path.name)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Helpers – scenario YAML (minimal, no external deps)
# ---------------------------------------------------------------------------

def _parse_scenario_links(yaml_path: Path) -> list[dict[str, str]]:
    """Extract ``topology.links`` entries from scenario.yaml.

    Returns a list of ``{"from": ..., "to": ..., "transport": ...}`` dicts.
    """
    if not yaml_path.exists():
        return []

    links: list[dict[str, str]] = []
    in_links = False
    current: dict[str, str] = {}

    for line in yaml_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        if stripped.startswith("links:"):
            in_links = True
            continue

        if not in_links:
            continue

        if stripped.startswith("- from:"):
            if current:
                links.append(current)
            current = {"from": stripped.split(":", 1)[1].strip()}
        elif stripped.startswith("to:") and current:
            current["to"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("transport:") and current:
            current["transport"] = stripped.split(":", 1)[1].strip()
        elif stripped and not stripped.startswith(("#", "to:", "transport:")):
            # New section reached – flush and stop.
            if current:
                links.append(current)
                current = {}
            in_links = False

    if current:
        links.append(current)
    return links


# ---------------------------------------------------------------------------
# Helpers – pair labels
# ---------------------------------------------------------------------------

def _pair_label(a: str, b: str) -> str:
    """Canonical pair label (alphabetical): ``'a ↔ b'``."""
    lo, hi = sorted((a, b))
    return f"{lo} ↔ {hi}"


# ---------------------------------------------------------------------------
# Helpers – hex node_addr → device alias mapping
# ---------------------------------------------------------------------------

def _build_hex_to_alias(
    timeseries: list[dict] | None,
    snapshot: dict | None,
) -> dict[str, str]:
    """Map hex ``node_addr`` → device alias from the first available source."""
    mapping: dict[str, str] = {}

    # Prefer timeseries (first sample) because it has the richest data.
    sources: list[dict[str, Any]] = []
    if timeseries:
        for sample in timeseries[:1]:
            sources.append(sample.get("devices", {}))
    if snapshot and not mapping:
        sources.append(snapshot)

    for src in sources:
        for alias, cmds in src.items():
            if not isinstance(cmds, dict):
                continue
            status = cmds.get("show_status") or {}
            addr = status.get("node_addr", "")
            if addr:
                mapping[addr] = alias
    return mapping


# ---------------------------------------------------------------------------
# Helpers – peer data collection
# ---------------------------------------------------------------------------

def _collect_peer_timeseries(
    timeseries: list[dict],
    hex_to_alias: dict[str, str],
) -> dict[str, dict[str, list[float]]]:
    """Collect per-pair ``loss_rate`` / ``srtt_ms`` across all time samples.

    Returns ``{pair_label: {"loss": [...], "rtt": [...]}}``.
    """
    pair_data: dict[str, dict[str, list[float]]] = {}

    for _sample in timeseries:
        devices = _sample.get("devices", {})
        for alias, cmds in devices.items():
            if not isinstance(cmds, dict):
                continue
            for peer in cmds.get("show_peers", {}).get("peers", []):
                peer_addr = peer.get("node_addr", "")
                peer_alias = hex_to_alias.get(peer_addr, peer_addr[:12])
                label = _pair_label(alias, peer_alias)

                mmp = peer.get("mmp") or {}
                loss = mmp.get("loss_rate")
                rtt = mmp.get("srtt_ms")

                bucket = pair_data.setdefault(label, {"loss": [], "rtt": []})
                if loss is not None:
                    bucket["loss"].append(float(loss))
                if rtt is not None:
                    bucket["rtt"].append(float(rtt))

    return pair_data


def _best_peer_sample(timeseries: list[dict]) -> dict[str, Any] | None:
    """Return the timeseries sample with the most observed peers."""
    best: dict[str, Any] | None = None
    best_count = -1
    for sample in timeseries:
        count = 0
        for cmds in sample.get("devices", {}).values():
            if isinstance(cmds, dict):
                count += len(cmds.get("show_peers", {}).get("peers", []))
        if count > best_count:
            best_count = count
            best = sample
    return best


def _build_connections(
    timeseries: list[dict],
    hex_to_alias: dict[str, str],
) -> list[dict]:
    """Connection summary from the timeseries sample with the most peers."""
    best = _best_peer_sample(timeseries) if timeseries else None
    if not best:
        return []

    connections: list[dict] = []
    seen: set[str] = set()

    for alias, cmds in best.get("devices", {}).items():
        if not isinstance(cmds, dict):
            continue
        for peer in cmds.get("show_peers", {}).get("peers", []):
            peer_addr = peer.get("node_addr", "")
            peer_alias = hex_to_alias.get(peer_addr, peer_addr[:12])
            label = _pair_label(alias, peer_alias)

            if label in seen:
                continue
            seen.add(label)

            connected = peer.get("connectivity") == "connected"
            stats = peer.get("stats") or {}
            connections.append(
                {
                    "pair": label,
                    "connected": connected,
                    "packets_sent": stats.get("packets_sent", 0),
                    "packets_recv": stats.get("packets_recv", 0),
                }
            )

    return connections


def _build_peer_metrics(
    pair_data: dict[str, dict[str, list[float]]],
) -> list[PeerMetrics]:
    """Aggregate per-pair metrics."""
    metrics: list[PeerMetrics] = []
    for label in sorted(pair_data):
        data = pair_data[label]
        losses = data["loss"]
        rtts = data["rtt"]

        metrics.append(
            PeerMetrics(
                pair=label,
                loss_min=min(losses) if losses else 0.0,
                loss_avg=(sum(losses) / len(losses)) if losses else 0.0,
                loss_max=max(losses) if losses else 0.0,
                rtt_min=min(rtts) if rtts else None,
                rtt_avg=(sum(rtts) / len(rtts)) if rtts else None,
                rtt_max=max(rtts) if rtts else None,
                samples=len(losses),
            )
        )
    return metrics


# ---------------------------------------------------------------------------
# Helpers – key exchange
# ---------------------------------------------------------------------------

def _build_key_exchange(
    keylog: dict | None,
    connections: list[dict],
) -> list[KeyExchangeInfo]:
    """Key exchange info per connected pair.

    Keylog data is per-device; we pick the first device (alphabetically)
    in the pair that has keylog entries.
    """
    if not keylog:
        return [
            KeyExchangeInfo(
                pair=c["pair"],
                link_keys=0,
                session_keys=0,
                coverage="⚠️ no keylog data",
            )
            for c in connections
        ]

    dev_keys: dict[str, dict[str, int]] = {}
    for alias, info in keylog.get("devices", {}).items():
        if isinstance(info, dict):
            dev_keys[alias] = {
                "link_keys": info.get("link_keys", 0),
                "session_keys": info.get("session_keys", 0),
            }

    results: list[KeyExchangeInfo] = []
    for conn in connections:
        parts = conn["pair"].split(" ↔ ")
        # Pick the first device (alphabetically) that has keylog data.
        info = dev_keys.get(parts[0]) or dev_keys.get(parts[1] if len(parts) > 1 else "")
        if info:
            lk, sk = info["link_keys"], info["session_keys"]
        else:
            lk, sk = 0, 0
        coverage = "✅" if (lk > 0 or sk > 0) else "⚠️ no keys"
        results.append(
            KeyExchangeInfo(pair=conn["pair"], link_keys=lk, session_keys=sk, coverage=coverage)
        )
    return results


# ---------------------------------------------------------------------------
# Helpers – rekey stats
# ---------------------------------------------------------------------------

def _build_rekey_stats(
    keylog: dict | None,
    connections: list[dict],
    duration_secs: int,
) -> list[RekeyStats]:
    """Rekey frequency per connected pair from keylog link_keys."""
    if not keylog or not connections:
        return []

    dev_keys: dict[str, int] = {}
    for alias, info in keylog.get("devices", {}).items():
        if isinstance(info, dict):
            dev_keys[alias] = info.get("link_keys", 0)

    results: list[RekeyStats] = []
    for conn in connections:
        parts = conn["pair"].split(" ↔ ")
        lk = dev_keys.get(parts[0], 0)
        if lk == 0 and len(parts) > 1:
            lk = dev_keys.get(parts[1], 0)

        total_rekeys = max(0, lk - 1)
        if duration_secs > 0 and total_rekeys > 0:
            avg_interval = duration_secs / total_rekeys
            rekeys_per_hour = (total_rekeys / duration_secs) * 3600
        elif total_rekeys == 0:
            avg_interval = None
            rekeys_per_hour = None
        else:
            avg_interval = None
            rekeys_per_hour = None

        results.append(RekeyStats(
            pair=conn["pair"],
            total_rekeys=total_rekeys,
            rekey_interval_avg_secs=avg_interval,
            rekeys_per_hour=rekeys_per_hour,
        ))
    return results


# ---------------------------------------------------------------------------
# Helpers – disconnect detection
# ---------------------------------------------------------------------------

def _detect_disconnects(
    timeseries: list[dict],
    hex_to_alias: dict[str, str],
) -> list[DisconnectEvent]:
    """Detect peer disappearances between consecutive timeseries samples.

    A disconnect is when a peer appears in sample N but NOT in sample N+1.
    If the peer reappears later, mark reconnected=True.
    """
    if len(timeseries) < 2:
        return []

    # {sample_idx: {alias: set(peer_addr)}}
    sample_peers: list[dict[str, set[str]]] = []
    for sample in timeseries:
        dev_peers: dict[str, set[str]] = {}
        for alias, cmds in sample.get("devices", {}).items():
            if not isinstance(cmds, dict):
                continue
            addrs: set[str] = set()
            for peer in cmds.get("show_peers", {}).get("peers", []):
                addr = peer.get("node_addr", "")
                if addr:
                    addrs.add(addr)
            dev_peers[alias] = addrs
        sample_peers.append(dev_peers)

    events: list[DisconnectEvent] = []
    seen: set[tuple[str, int]] = set()

    for i in range(len(sample_peers) - 1):
        curr = sample_peers[i]
        nxt = sample_peers[i + 1]
        nxt_t = timeseries[i + 1].get("t", 0)

        for alias, curr_addrs in curr.items():
            nxt_addrs = nxt.get(alias, set())
            disappeared = curr_addrs - nxt_addrs
            for addr in disappeared:
                peer_alias = hex_to_alias.get(addr, addr[:12])
                label = _pair_label(alias, peer_alias)
                key = (label, nxt_t)
                if key in seen:
                    continue
                seen.add(key)

                reconnected = any(
                    addr in sample_peers[j].get(alias, set())
                    for j in range(i + 2, len(sample_peers))
                )

                events.append(DisconnectEvent(
                    pair=label,
                    t=nxt_t,
                    reconnected=reconnected,
                ))

    return events


# ---------------------------------------------------------------------------
# Helpers – keylog verification
# ---------------------------------------------------------------------------

def _build_keylog_verification(
    keylog: dict | None,
    connections: list[dict],
    run_dir: Path,
) -> list[KeylogVerification]:
    """Verify keylog files are parseable and ready for decryption."""
    if not keylog:
        return []

    results: list[KeylogVerification] = []
    for conn in connections:
        parts = conn["pair"].split(" ↔ ")
        best: KeylogVerification | None = None
        for alias in parts:
            dev_info = keylog.get("devices", {}).get(alias)
            if not isinstance(dev_info, dict):
                continue

            link_keys = dev_info.get("link_keys", 0)

            local_keys_parsed = 0
            parse_errors = 0
            unique_peer_pairs = 0

            keylog_path = run_dir / f"keylog-{alias}.txt"
            if keylog_path.exists():
                try:
                    from lab.capture.keylog import parse_keylog
                    parsed = parse_keylog(keylog_path)
                    local_keys_parsed = len(parsed.valid_entries)
                    parse_errors = len(parsed.parse_errors)
                    unique_peer_pairs = len(parsed.peer_pairs)
                except Exception:
                    parse_errors = dev_info.get("parse_errors", 0)
                    local_keys_parsed = link_keys

            decryption_ready = link_keys > 0 and parse_errors == 0

            if decryption_ready:
                note = "Ready for Wireshark decryption (needs fips-dissector.lua)"
            elif parse_errors > 0:
                note = f"Parse errors: {parse_errors}"
            elif link_keys == 0:
                note = "No link keys found"
            else:
                note = "Incomplete key data"

            entry = KeylogVerification(
                pair=conn["pair"],
                local_keys_parsed=local_keys_parsed,
                parse_errors=parse_errors,
                unique_peer_pairs=unique_peer_pairs,
                decryption_ready=decryption_ready,
                note=note,
            )

            if best is None or entry.local_keys_parsed > best.local_keys_parsed:
                best = entry

        if best:
            results.append(best)

    return results


# ---------------------------------------------------------------------------
# Helpers – captures
# ---------------------------------------------------------------------------

def _build_captures(captures: dict | None) -> dict:
    """Capture summary keyed by capture type (derived from filename)."""
    if not captures:
        return {}
    result: dict[str, dict] = {}
    for _alias, info in captures.items():
        if not isinstance(info, dict):
            continue
        if not info.get("enabled", True):
            continue
        file_path = info.get("file")
        if not file_path:
            continue
        capture_type = Path(file_path).stem
        result[capture_type] = {
            "device": info.get("device", ""),
            "size_bytes": info.get("size_bytes", 0),
        }
    return result


# ---------------------------------------------------------------------------
# Helpers – error detection
# ---------------------------------------------------------------------------

def _has_timeseries_errors(timeseries: list[dict]) -> bool:
    """Return True if any timeseries entry contains an error key."""
    for sample in timeseries:
        for _alias, cmds in sample.get("devices", {}).items():
            if not isinstance(cmds, dict):
                continue
            for key in cmds:
                if "error" in key.lower():
                    return True
    return False


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

def _evaluate_assertions(
    connections: list[dict],
    peer_metrics: list[PeerMetrics],
    key_exchange: list[KeyExchangeInfo],
    expected_links: list[dict[str, str]],
    has_errors: bool,
    disconnects: list[DisconnectEvent] | None = None,
) -> list[AssertionResult]:
    """Evaluate the hardcoded default assertion set."""
    results: list[AssertionResult] = []

    connected_pairs = {c["pair"] for c in connections if c["connected"]}

    # --- 1. All expected peer pairs connected ---
    missing: list[str] = []
    for link in expected_links:
        pair = _pair_label(link["from"], link["to"])
        if pair not in connected_pairs:
            missing.append(pair)

    if expected_links:
        if missing:
            results.append(
                AssertionResult(
                    name="All expected peers connected",
                    expected=f"{len(expected_links)} links",
                    actual=f"missing: {', '.join(missing)}",
                    passed=False,
                )
            )
        else:
            results.append(
                AssertionResult(
                    name="All expected peers connected",
                    expected=f"{len(expected_links)} links",
                    actual=f"{len(connected_pairs)} connected",
                    passed=True,
                )
            )

    # --- 2. MMP loss < 5 % ---
    max_loss_ratio = max((pm.loss_max for pm in peer_metrics), default=0.0)
    # loss_rate is a ratio 0.0–1.0 in the FIPS data
    max_loss_pct = max_loss_ratio * 100.0
    results.append(
        AssertionResult(
            name="MMP loss < 5%",
            expected="< 5.0%",
            actual=f"{max_loss_pct:.1f}%",
            passed=max_loss_pct < 5.0,
        )
    )

    # --- 3. Keylog coverage ---
    any_connected = bool(connections)
    if any_connected:
        total = len(key_exchange)
        covered = sum(1 for ke in key_exchange if ke.coverage == "✅")
        results.append(
            AssertionResult(
                name="Keylog coverage",
                expected="100%",
                actual=f"{covered}/{total}",
                passed=covered == total,
            )
        )

    # --- 4. No test loop errors ---
    results.append(
        AssertionResult(
            name="No loop errors",
            expected="0 errors",
            actual="errors detected" if has_errors else "0 errors",
            passed=not has_errors,
        )
    )

    # --- 5. No disconnects ---
    disc = disconnects if disconnects is not None else []
    results.append(
        AssertionResult(
            name="No disconnects",
            expected="0 disconnects",
            actual=f"{len(disc)} disconnect(s)" if disc else "0 disconnects",
            passed=len(disc) == 0,
        )
    )

    return results


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def _determine_verdict(
    assertions: list[AssertionResult],
    connections: list[dict],
    has_timeseries: bool,
) -> str:
    if not has_timeseries or not connections:
        return "INSUFFICIENT_DATA"

    all_passed = all(a.passed for a in assertions)
    if all_passed:
        return "PASS"

    # If all connectivity assertions pass but metric assertions fail → DEGRADED
    connectivity_ok = all(
        a.passed for a in assertions if "connected" in a.name.lower()
    )
    return "DEGRADED" if connectivity_ok else "FAIL"


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

def _format_duration(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_run(run_dir: Path) -> AnalysisReport:
    """Parse all artifacts in *run_dir* and produce structured analysis."""

    run_dir = Path(run_dir)

    # -- load artifacts -----------------------------------------------------
    metadata = _load_json(run_dir / "metadata.json") or {}
    timeseries: list[dict] = _load_json(run_dir / "metrics-timeseries.json") or []  # type: ignore[assignment]
    snapshot = _load_json(run_dir / "snapshot-initial.json")
    keylog = _load_json(run_dir / "keylog-results.json")
    captures_raw = _load_json(run_dir / "capture-results.json")

    # -- metadata -----------------------------------------------------------
    scenario_name = metadata.get("scenario", "")
    if not scenario_name and "-" in run_dir.name:
        scenario_name = run_dir.name.split("-", 2)[-1]
    timestamp = metadata.get("timestamp", "")
    duration_secs = metadata.get("duration_secs", 0)

    # -- hex → alias map ----------------------------------------------------
    hex_to_alias = _build_hex_to_alias(timeseries or None, snapshot)

    # -- peer metrics -------------------------------------------------------
    pair_data = _collect_peer_timeseries(timeseries, hex_to_alias) if timeseries else {}
    peer_metrics = _build_peer_metrics(pair_data)

    # -- connections --------------------------------------------------------
    connections = _build_connections(timeseries, hex_to_alias)

    # -- key exchange -------------------------------------------------------
    key_exchange = _build_key_exchange(keylog, connections)

    # -- rekey stats --------------------------------------------------------
    rekey_stats = _build_rekey_stats(keylog, connections, duration_secs)

    # -- disconnect detection -----------------------------------------------
    disconnects = _detect_disconnects(timeseries, hex_to_alias) if timeseries else []

    # -- keylog verification ------------------------------------------------
    keylog_verification = _build_keylog_verification(keylog, connections, run_dir)

    # -- captures -----------------------------------------------------------
    captures_summary = _build_captures(captures_raw)

    # -- scenario links (for connectivity assertions) ----------------------
    expected_links = _parse_scenario_links(run_dir / "scenario.yaml")

    # -- error detection ----------------------------------------------------
    has_errors = _has_timeseries_errors(timeseries)

    # -- assertions ---------------------------------------------------------
    assertions = _evaluate_assertions(
        connections, peer_metrics, key_exchange, expected_links, has_errors,
        disconnects=disconnects,
    )

    # -- verdict ------------------------------------------------------------
    verdict = _determine_verdict(assertions, connections, bool(timeseries))

    return AnalysisReport(
        scenario_name=scenario_name,
        timestamp=timestamp,
        duration_secs=duration_secs,
        verdict=verdict,
        connections=connections,
        peer_metrics=peer_metrics,
        key_exchange=key_exchange,
        captures=captures_summary,
        rekey_stats=rekey_stats,
        disconnects=disconnects,
        keylog_verification=keylog_verification,
        assertions=assertions,
        memory={},  # RSS not available in current artifacts
    )


def write_analysis(report: AnalysisReport, run_dir: Path) -> None:
    """Write ``analysis.json`` and ``analysis.md`` to *run_dir*."""

    run_dir = Path(run_dir)

    # -- JSON ---------------------------------------------------------------
    json_data = {
        "scenario_name": report.scenario_name,
        "timestamp": report.timestamp,
        "duration_secs": report.duration_secs,
        "verdict": report.verdict,
        "connections": report.connections,
        "peer_metrics": [asdict(pm) for pm in report.peer_metrics],
        "key_exchange": [asdict(ke) for ke in report.key_exchange],
        "captures": report.captures,
        "rekey_stats": [asdict(rs) for rs in report.rekey_stats],
        "disconnects": [asdict(d) for d in report.disconnects],
        "keylog_verification": [asdict(kv) for kv in report.keylog_verification],
        "assertions": [asdict(a) for a in report.assertions],
        "memory": report.memory,
    }
    json_path = run_dir / "analysis.json"
    json_path.write_text(json.dumps(json_data, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %s", json_path)

    # -- Markdown -----------------------------------------------------------
    md_path = run_dir / "analysis.md"
    md_path.write_text(format_markdown(report), encoding="utf-8")
    logger.info("Wrote %s", md_path)


def format_markdown(report: AnalysisReport) -> str:
    """Format *report* as human-readable markdown."""
    lines: list[str] = []

    # -- header -------------------------------------------------------------
    verdict_icon = {"PASS": "✅", "FAIL": "❌", "DEGRADED": "⚠️", "INSUFFICIENT_DATA": "❓"}.get(
        report.verdict, ""
    )
    lines.append(f"# FIPS Lab Test Report — {report.scenario_name}")
    lines.append(
        f"**Date**: {report.timestamp} | **Duration**: {_format_duration(report.duration_secs)}"
        f" | **Verdict**: {verdict_icon} {report.verdict}"
    )
    lines.append("")

    # -- connections --------------------------------------------------------
    lines.append("## Connections")
    lines.append("| Pair | Connected | Packets Sent | Packets Recv |")
    lines.append("|------|-----------|-------------|-------------|")
    for c in report.connections:
        icon = "✅" if c["connected"] else "❌"
        lines.append(f"| {c['pair']} | {icon} | {c['packets_sent']} | {c['packets_recv']} |")
    lines.append("")

    # -- MMP metrics --------------------------------------------------------
    lines.append("## MMP Metrics")
    lines.append("| Pair | Loss (min/avg/max) | RTT (min/avg/max) | Samples |")
    lines.append("|------|--------------------|--------------------|---------|")
    for pm in report.peer_metrics:
        loss_str = f"{pm.loss_min * 100:.1f}% / {pm.loss_avg * 100:.1f}% / {pm.loss_max * 100:.1f}%"
        if pm.rtt_min is not None and pm.rtt_avg is not None and pm.rtt_max is not None:
            rtt_str = f"{pm.rtt_min:.0f}ms / {pm.rtt_avg:.0f}ms / {pm.rtt_max:.0f}ms"
        else:
            rtt_str = "N/A"
        lines.append(f"| {pm.pair} | {loss_str} | {rtt_str} | {pm.samples} |")
    lines.append("")

    # -- key exchange -------------------------------------------------------
    lines.append("## Key Exchange")
    lines.append("| Pair | Link Keys | Session Keys | Coverage |")
    lines.append("|------|-----------|--------------|----------|")
    for ke in report.key_exchange:
        lines.append(f"| {ke.pair} | {ke.link_keys} | {ke.session_keys} | {ke.coverage} |")
    lines.append("")

    # -- captures -----------------------------------------------------------
    lines.append("## Captures")
    lines.append("| Type | Device | Size |")
    lines.append("|------|--------|------|")
    for ctype, info in report.captures.items():
        size_kb = info.get("size_bytes", 0) / 1024
        lines.append(f"| {ctype} | {info.get('device', '')} | {size_kb:.0f} KB |")
    lines.append("")

    # -- rekey stats --------------------------------------------------------
    if report.rekey_stats:
        lines.append("## Rekey Analysis")
        lines.append("| Pair | Rekeys | Avg Interval | Rekeys/Hour |")
        lines.append("|------|--------|-------------|-------------|")
        for rs in report.rekey_stats:
            interval = f"{rs.rekey_interval_avg_secs:.1f}s" if rs.rekey_interval_avg_secs else "N/A"
            rph = f"{rs.rekeys_per_hour:.0f}" if rs.rekeys_per_hour else "N/A"
            lines.append(f"| {rs.pair} | {rs.total_rekeys} | {interval} | {rph} |")
        lines.append("")

    # -- disconnects --------------------------------------------------------
    lines.append("## Disconnects")
    lines.append("| Pair | Time | Reconnected |")
    lines.append("|------|------|-------------|")
    if report.disconnects:
        for d in report.disconnects:
            rc = "✅" if d.reconnected else "❌"
            lines.append(f"| {d.pair} | {d.t}s | {rc} |")
    else:
        lines.append("| *(none)* | | |")
    lines.append("")

    # -- keylog verification ------------------------------------------------
    if report.keylog_verification:
        lines.append("## Keylog Verification")
        lines.append("| Pair | Keys Parsed | Parse Errors | Decryption Ready |")
        lines.append("|------|------------|-------------|-----------------|")
        for kv in report.keylog_verification:
            ready = "✅" if kv.decryption_ready else "❌"
            lines.append(f"| {kv.pair} | {kv.local_keys_parsed} | {kv.parse_errors} | {ready} |")
        lines.append("")

    # -- assertions ---------------------------------------------------------
    lines.append("## Assertions")
    lines.append("| Check | Expected | Actual | Result |")
    lines.append("|-------|----------|--------|--------|")
    for a in report.assertions:
        tag = "✅ PASS" if a.passed else "❌ FAIL"
        lines.append(f"| {a.name} | {a.expected} | {a.actual} | {tag} |")
    lines.append("")

    # -- memory -------------------------------------------------------------
    if report.memory:
        lines.append("## Memory")
        lines.append("| Device | RSS |")
        lines.append("|--------|-----|")
        for device, rss in report.memory.items():
            lines.append(f"| {device} | {rss} |")
        lines.append("")

    return "\n".join(lines)
