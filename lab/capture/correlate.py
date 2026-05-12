#!/usr/bin/env python3
"""Correlate btsnoop pcap, BLE event logs, and keylog timestamps.

Reads the three data sources from a fips-lab results directory and produces
a unified timeline in JSONL format with normalized Unix-epoch timestamps.

Usage:
    python3 lab/capture/correlate.py results/20260510-174116-lab-2node-ble-20min

Outputs:
    <results_dir>/correlated-timeline.jsonl   — unified event stream
    <results_dir>/correlation-summary.md       — human-readable summary
"""
from __future__ import annotations

import json
import logging
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# btsnoop epoch: 2000-01-01 00:00:00 UTC
# Offset from Unix epoch (1970-01-01) in microseconds.
BTSNOOP_EPOCH_OFFSET_US = 946684800_000000

BTSNOOP_MAGIC = b"btsnoop\x00"
BTSNOOP_VERSION = 1
HCI_MONITOR_DATALINK = 2001
HCI_ACL_DATA = 0x02

L2CAP_SIGNALLING_CID = 0x0001
L2CAP_ATT_CID = 0x0004
L2CAP_SMP_CID = 0x0006
L2CAP_DYNAMIC_CID_MIN = 0x0040

L2CAP_CONN_REQ = 0x02
L2CAP_DISCONN_REQ = 0x06


def btsnoop_ts_to_unix_us(ts_us: int) -> int:
    return ts_us - BTSNOOP_EPOCH_OFFSET_US


def unix_us_to_iso(unix_us: int) -> str:
    secs = unix_us // 1_000_000
    micros = unix_us % 1_000_000
    dt = datetime.fromtimestamp(secs, tz=timezone.utc)
    return dt.strftime(f"%Y-%m-%dT%H:%M:%S.{micros:06d}Z")


def parse_iso_to_unix_us(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000)


# ============================================================================
# btsnoop parser (lightweight — only extracts L2CAP signalling events)
# ============================================================================

def parse_btsnoop_l2cap_events(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    if len(raw) < 24 or raw[:8] != BTSNOOP_MAGIC:
        return []

    records: list[dict[str, Any]] = []
    offset = 16
    prev_ts_us = 0

    while offset + 24 <= len(raw):
        inc_len = struct.unpack(">I", raw[offset + 4:offset + 8])[0]
        ts_us = struct.unpack(">Q", raw[offset + 16:offset + 24])[0]
        data = raw[offset + 24:offset + 24 + inc_len]

        # Skip duplicate timestamps (btmon sometimes emits them)
        if ts_us == prev_ts_us:
            offset += 24 + inc_len
            continue
        prev_ts_us = ts_us

        unix_us = btsnoop_ts_to_unix_us(ts_us)

        # btmon monitor mode: first byte is HCI packet type indicator
        if len(data) < 2:
            offset += 24 + inc_len
            continue

        pkt_type = data[0]
        if pkt_type != HCI_ACL_DATA:
            offset += 24 + inc_len
            continue

        # ACL header: 4 bytes (handle + length)
        if len(data) < 9:
            offset += 24 + inc_len
            continue

        acl_handle = struct.unpack("<H", data[1:3])[0] & 0x0FFF
        acl_len = struct.unpack("<H", data[3:5])[0]
        pb_flag = (struct.unpack("<H", data[1:3])[0] >> 12) & 0x03

        # Only first-fragment packets carry L2CAP header
        if pb_flag != 0:
            offset += 24 + inc_len
            continue

        # L2CAP header: 4 bytes (length + CID)
        l2cap_len = struct.unpack("<H", data[5:7])[0]
        l2cap_cid = struct.unpack("<H", data[7:9])[0]

        # L2CAP signalling channel events
        if l2cap_cid == L2CAP_SIGNALLING_CID and len(data) > 12:
            code = data[9]
            ident = data[10]
            sig_len = struct.unpack("<H", data[11:13])[0]

            if code == L2CAP_CONN_REQ and len(data) > 16:
                psm = struct.unpack("<H", data[13:15])[0]
                scid = struct.unpack("<H", data[15:17])[0]
                records.append({
                    "source": "pcap",
                    "ts_us": unix_us,
                    "ts_iso": unix_us_to_iso(unix_us),
                    "event": "l2cap_conn_req",
                    "psm": psm,
                    "scid": scid,
                    "handle": acl_handle,
                })
            elif code == L2CAP_DISCONN_REQ and len(data) > 16:
                dcid = struct.unpack("<H", data[13:15])[0]
                scid = struct.unpack("<H", data[15:17])[0]
                records.append({
                    "source": "pcap",
                    "ts_us": unix_us,
                    "ts_iso": unix_us_to_iso(unix_us),
                    "event": "l2cap_disconn_req",
                    "dcid": dcid,
                    "scid": scid,
                    "handle": acl_handle,
                })

        offset += 24 + inc_len

    return records


# ============================================================================
# Event log parser
# ============================================================================

def parse_event_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text().strip().splitlines():
        try:
            entry = json.loads(line)
            entry["source"] = "event_log"
            entry["ts_us"] = parse_iso_to_unix_us(entry["ts"])
            events.append(entry)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return events


# ============================================================================
# Keylog parser (timestamps from file modification are approximate)
# ============================================================================

def parse_keylog_events(path: Path, alias: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for i, line in enumerate(path.read_text().strip().splitlines()):
        parts = line.strip().split()
        if len(parts) < 5:
            continue
        events.append({
            "source": "keylog",
            "ts_us": 0,
            "ts_iso": "",
            "event": f"noise_{parts[0].lower()}_handshake",
            "alias": alias,
            "local_npub": parts[1][:16] + "...",
            "peer_npub": parts[2][:16] + "...",
            "index": i,
        })
    return events


# ============================================================================
# Correlate
# ============================================================================

def correlate(results_dir: Path) -> dict[str, Any]:
    btsnoop_path = results_dir / "btmon.btsnoop"
    event_logs = sorted(results_dir.glob("ble-events-*.jsonl"))
    keylogs = sorted(results_dir.glob("keylog-*.txt"))

    all_events: list[dict[str, Any]] = []

    # 1. btsnoop L2CAP signalling events
    if btsnoop_path.exists():
        pcap_events = parse_btsnoop_l2cap_events(btsnoop_path)
        all_events.extend(pcap_events)
        log.info("pcap: %d L2CAP signalling events", len(pcap_events))

    # 2. BLE event logs
    for el_path in event_logs:
        alias = el_path.stem.replace("ble-events-", "")
        evts = parse_event_log(el_path)
        for e in evts:
            e["alias"] = alias
        all_events.extend(evts)
        log.info("event_log %s: %d events", alias, len(evts))

    # 3. Keylog entries (no timestamps — count only)
    total_keylog = 0
    for kl_path in keylogs:
        alias = kl_path.stem.replace("keylog-", "")
        kl_events = parse_keylog_events(kl_path, alias)
        total_keylog += len(kl_events)
    log.info("keylog: %d total handshake entries (no timestamps)", total_keylog)

    # Sort by timestamp (keylog entries with ts_us=0 go first)
    all_events.sort(key=lambda e: e.get("ts_us", 0))

    # Write correlated timeline
    timeline_path = results_dir / "correlated-timeline.jsonl"
    with open(timeline_path, "w") as f:
        for e in all_events:
            f.write(json.dumps(e, default=str) + "\n")

    # Summary
    event_counts: dict[str, int] = {}
    for e in all_events:
        evt = e.get("event", "unknown")
        event_counts[evt] = event_counts.get(evt, 0) + 1

    # RTT progression from event logs
    rtt_samples = [
        e for e in all_events
        if e.get("event") == "mmp_rtt_sample" and e.get("ts_us", 0) > 0
    ]
    rtt_summary = ""
    if rtt_samples:
        first_rtt = rtt_samples[0]
        last_rtt = rtt_samples[-1]
        min_rtt = min(int(e.get("rtt_ms", 0)) for e in rtt_samples)
        max_rtt = max(int(e.get("rtt_ms", 0)) for e in rtt_samples)
        rtt_summary = (
            f"\n### RTT Progression\n"
            f"- Samples: {len(rtt_samples)}\n"
            f"- First: {first_rtt.get('rtt_ms')}ms (SRTT {first_rtt.get('srtt_ms')}ms) at {first_rtt.get('ts') or first_rtt.get('ts_iso')}\n"
            f"- Last: {last_rtt.get('rtt_ms')}ms (SRTT {last_rtt.get('srtt_ms')}ms) at {last_rtt.get('ts') or last_rtt.get('ts_iso')}\n"
            f"- Range: {min_rtt}ms — {max_rtt}ms\n"
        )

    # Disconnect timeline
    disconnects = [e for e in all_events if e.get("event") == "ble_disconnect"]
    disc_summary = ""
    if disconnects:
        disc_summary = "\n### Disconnect Events\n"
        for d in disconnects:
            ts = d.get("ts") or d.get("ts_iso") or "?"
            disc_summary += f"- {ts} [{d.get('alias','?')}] {d.get('reason','?')} uptime={d.get('uptime_secs','?')}s\n"

    summary = (
        f"# Correlation Summary: {results_dir.name}\n\n"
        f"## Sources\n"
        f"- btsnoop: {btsnoop_path.name} ({'found' if btsnoop_path.exists() else 'not found'})\n"
        f"- event logs: {len(event_logs)} files\n"
        f"- keylogs: {total_keylog} handshake entries\n\n"
        f"## Event Counts\n"
    )
    for evt, count in sorted(event_counts.items()):
        summary += f"- {evt}: {count}\n"

    summary += f"\n## Timeline\n"
    summary += f"- Total correlated events: {len(all_events)}\n"
    summary += f"- Output: `correlated-timeline.jsonl`\n"
    summary += rtt_summary
    summary += disc_summary

    summary_path = results_dir / "correlation-summary.md"
    summary_path.write_text(summary)
    log.info("Wrote %s (%d events)", timeline_path, len(all_events))
    log.info("Wrote %s", summary_path)

    return {
        "total_events": len(all_events),
        "event_counts": event_counts,
        "rtt_samples": len(rtt_samples),
        "disconnects": len(disconnects),
        "keylog_entries": total_keylog,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir>")
        sys.exit(1)
    results_dir = Path(sys.argv[1])
    if not results_dir.is_dir():
        print(f"Not a directory: {results_dir}")
        sys.exit(1)
    result = correlate(results_dir)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
