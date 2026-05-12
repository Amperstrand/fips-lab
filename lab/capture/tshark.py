"""Post-test tshark capture analysis for fips-lab.

Runs tshark on btsnoop captures to produce BLE statistics summaries.
All raw tshark output goes into a tshark-raw/ directory (gitignored).
Only sanitized summary statistics (no addresses, keys, or payloads) are
published to tshark-summary.json and tshark-summary.md.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TSHARK_TIMEOUT = 60  # seconds per invocation


# ============================================================================
# Helpers — tshark invocation
# ============================================================================

def _run_tshark(
    btsnoop: Path,
    args: list[str],
    output_path: Path,
) -> subprocess.CompletedProcess[str] | None:
    """Run tshark with given args, capture stdout to output_path.

    Returns the CompletedProcess on success, None on failure.
    """
    cmd = ["tshark", "-r", str(btsnoop)] + args
    log.info("tshark: %s", " ".join(args))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TSHARK_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("tshark timed out after %ds: %s", TSHARK_TIMEOUT, " ".join(args))
        return None
    except FileNotFoundError:
        log.warning("tshark not found on PATH")
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.stdout, encoding="utf-8")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # tshark may return non-zero for empty captures — log but don't fail
        log.warning("tshark exited %d: %s", result.returncode, stderr[:200] if stderr else "(no stderr)")
        # Still return the result if we got stdout
        if not result.stdout.strip():
            return None

    return result


# ============================================================================
# Parsers — extract sanitized stats from raw tshark output
# ============================================================================

def _parse_io_stats(text: str) -> dict[str, Any]:
    """Parse tshark -q -z io,stat,1 output for BLE frame statistics.

    Example output:
        |    Interval     | Frames | ...
        | 0.0-1.0         |     45 | ...
    """
    stats: dict[str, Any] = {
        "total_frames": 0,
        "interval_stats": [],
    }

    # Look for the summary table
    for line in text.splitlines():
        # Match data lines like: | 0.0-1.0         |     45 |
        m = re.match(r"\s*\|\s*[\d.]+[:-][\d.]+\s*\|\s*(\d+)", line)
        if m:
            count = int(m.group(1))
            stats["total_frames"] += count
            stats["interval_stats"].append({"frames": count})

    # Also check for total line
    m = re.search(r"Frames:\s*(\d+)", text)
    if m:
        stats["total_frames"] = int(m.group(1))

    return stats


def _parse_l2cap_conversations(text: str) -> dict[str, Any]:
    """Parse tshark -q -z conv,btl2cap output for L2CAP conversation stats.

    Returns sanitized counts (no addresses).
    """
    stats: dict[str, Any] = {
        "total_conversations": 0,
        "total_frames": 0,
        "total_bytes": 0,
        "conversations": [],
    }

    in_table = False
    for line in text.splitlines():
        # Table starts after separator line
        if "=====" in line:
            in_table = not in_table
            continue

        if not in_table:
            continue

        # Parse: addr1:port <-> addr2:port  frames  bytes  ...
        # We only care about aggregate counts, not addresses
        parts = line.split()
        if len(parts) >= 6:
            try:
                # frames and bytes are typically columns 3-4 or similar
                # Count data rows
                stats["total_conversations"] += 1
            except (ValueError, IndexError):
                continue

    # Also try to get totals from the bottom of the output
    # Look for lines like "Total conversations: N"
    m = re.search(r"Total\s+.*?:\s*(\d+)", text)
    if m:
        stats["total_conversations"] = int(m.group(1))

    return stats


def _parse_fmp_frames_json(text: str) -> dict[str, Any]:
    """Parse tshark FMP JSON output (PSM 133 filtered) for frame counts.

    Returns counts only — no raw payloads or addresses.
    """
    stats: dict[str, Any] = {
        "fmp_frame_count": 0,
        "errors": [],
    }

    if not text.strip():
        return stats

    try:
        frames = json.loads(text)
    except json.JSONDecodeError as exc:
        stats["errors"].append(f"JSON parse error: {exc}")
        return stats

    if isinstance(frames, list):
        stats["fmp_frame_count"] = len(frames)
    elif isinstance(frames, dict):
        # Single frame or error response
        stats["fmp_frame_count"] = 1

    return stats


def _parse_hci_summary(text: str) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "error_count": 0,
        "warn_count": 0,
        "note_count": 0,
        "details": [],
    }

    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Errors"):
            m = re.search(r"\((\d+)\)", stripped)
            if m:
                stats["error_count"] = int(m.group(1))
            section = "error"
        elif stripped.startswith("Warn"):
            m = re.search(r"\((\d+)\)", stripped)
            if m:
                stats["warn_count"] = int(m.group(1))
            section = "warn"
        elif stripped.startswith("Note"):
            m = re.search(r"\((\d+)\)", stripped)
            if m:
                stats["note_count"] = int(m.group(1))
            section = "note"
        elif section and stripped and not stripped.startswith("=") and not stripped.startswith("Frequency"):
            freq_m = re.match(r"\s*(\d+)\s+", stripped)
            if freq_m:
                freq = int(freq_m.group(1))
                summary_text = stripped[freq_m.end():].strip()
                if section == "error" and freq > 0:
                    stats["details"].append(f"[error x{freq}] {summary_text}")

    return stats


# ============================================================================
# Frame size analysis
# ============================================================================

def _compute_frame_sizes(raw_dir: Path) -> dict[str, Any]:
    """Compute average/peak frame sizes from the io-stats or hci-summary.

    Returns aggregated stats only — no per-frame data.
    """
    sizes: dict[str, Any] = {
        "avg_frame_size_bytes": None,
        "peak_frame_size_bytes": None,
    }

    # Try to extract from io-stats.txt
    io_path = raw_dir / "io-stats.txt"
    if io_path.exists():
        text = io_path.read_text(encoding="utf-8")
        # Look for bytes/frame patterns
        byte_values = []
        for m in re.finditer(r"(\d+)\s+bytes", text):
            byte_values.append(int(m.group(1)))
        if byte_values:
            sizes["peak_frame_size_bytes"] = max(byte_values)
            sizes["avg_frame_size_bytes"] = round(
                sum(byte_values) / len(byte_values), 1
            )

    return sizes


# ============================================================================
# Summary writers
# ============================================================================

def _write_json(summary: dict[str, Any], path: Path) -> None:
    """Write summary as JSON."""
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def _write_markdown(summary: dict[str, Any], path: Path) -> None:
    """Write summary as human-readable markdown."""
    lines: list[str] = ["# TShark BLE Statistics", ""]

    lines.append(f"- **Capture**: `{summary.get('capture_file', '')}`")
    lines.append(f"- **TShark available**: {summary.get('tshark_available', False)}")

    if not summary.get("tshark_available"):
        lines.append("")
        lines.append("*tshark not found on PATH — analysis skipped.*")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    if summary.get("errors"):
        lines.append("")
        lines.append("### Errors")
        for err in summary["errors"]:
            lines.append(f"- {err}")

    # HCI statistics
    hci = summary.get("hci_summary", {})
    if hci:
        lines.append("")
        lines.append("## Expert Analysis")
        lines.append("| Severity | Count |")
        lines.append("|----------|-------|")
        lines.append(f"| Errors | {hci.get('error_count', 0)} |")
        lines.append(f"| Warnings | {hci.get('warn_count', 0)} |")
        lines.append(f"| Notes | {hci.get('note_count', 0)} |")
        if hci.get("details"):
            for d in hci["details"][:5]:
                lines.append(f"  - {d}")

    # IO statistics
    io = summary.get("io_stats", {})
    if io:
        lines.append("")
        lines.append("## IO Statistics")
        lines.append(f"- **Total frames**: {io.get('total_frames', 0)}")

    # L2CAP conversations
    l2cap = summary.get("l2cap_conversations", {})
    if l2cap:
        lines.append("")
        lines.append("## L2CAP Conversations")
        lines.append(f"- **Total conversations**: {l2cap.get('total_conversations', 0)}")

    # FMP frames
    fmp = summary.get("fmp_frames", {})
    if fmp:
        lines.append("")
        lines.append("## FMP Frames (PSM 133)")
        lines.append(f"- **FMP frame count**: {fmp.get('fmp_frame_count', 0)}")

    # Frame sizes
    fs = summary.get("frame_sizes", {})
    if fs and (fs.get("avg_frame_size_bytes") or fs.get("peak_frame_size_bytes")):
        lines.append("")
        lines.append("## Frame Sizes")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        if fs.get("avg_frame_size_bytes") is not None:
            lines.append(f"| Average | {fs['avg_frame_size_bytes']:.1f} bytes |")
        if fs.get("peak_frame_size_bytes") is not None:
            lines.append(f"| Peak | {fs['peak_frame_size_bytes']} bytes |")

    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ============================================================================
# Public API
# ============================================================================

def run_tshark_analysis(run_dir: Path) -> dict | None:
    """Run tshark on the btsnoop capture and produce sanitized summary stats.

    Looks for btmon.btsnoop in run_dir. Creates tshark-raw/ for full output
    (gitignored) and tshark-summary.json / .md for sanitized summaries.

    Returns the summary dict, or None if tshark is unavailable or no capture.
    """
    run_dir = Path(run_dir)
    btsnoop = run_dir / "btmon.btsnoop"

    # Check btsnoop exists
    if not btsnoop.exists():
        log.info("no btsnoop capture found in %s, skipping tshark analysis", run_dir)
        return None

    # Check tshark is available
    tshark_path = shutil.which("tshark")
    if not tshark_path:
        log.info("tshark not found on PATH, skipping tshark analysis")
        return None

    log.info("tshark found at %s", tshark_path)

    raw_dir = run_dir / "tshark-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "capture_file": btsnoop.name,
        "tshark_available": True,
        "errors": [],
        "io_stats": {},
        "l2cap_conversations": {},
        "fmp_frames": {},
        "hci_summary": {},
        "frame_sizes": {},
    }

    # --- IO statistics: tshark -r btmon.btsnoop -q -z io,stat,1 ---
    io_result = _run_tshark(
        btsnoop, ["-q", "-z", "io,stat,1"], raw_dir / "io-stats.txt",
    )
    if io_result and io_result.stdout:
        summary["io_stats"] = _parse_io_stats(io_result.stdout)
    else:
        summary["errors"].append("io-stats: tshark produced no output")

    # --- L2CAP conversations: tshark -r btmon.btsnoop -q -z conv,bluetooth ---
    l2cap_result = _run_tshark(
        btsnoop, ["-q", "-z", "conv,bluetooth"], raw_dir / "bluetooth-conversations.txt",
    )
    if l2cap_result and l2cap_result.stdout:
        summary["l2cap_conversations"] = _parse_l2cap_conversations(l2cap_result.stdout)
    else:
        summary["errors"].append("bluetooth-conversations: tshark produced no output")

    # --- FMP frames (PSM 133): tshark -r btmon.btsnoop -Y "btl2cap.psm == 133" -T json ---
    fmp_result = _run_tshark(
        btsnoop,
        ["-Y", "btl2cap.psm == 133", "-T", "json"],
        raw_dir / "fmp-frames.json",
    )
    if fmp_result and fmp_result.stdout:
        summary["fmp_frames"] = _parse_fmp_frames_json(fmp_result.stdout)
    else:
        summary["errors"].append("fmp-frames: tshark produced no output (no PSM 133 traffic or filter unsupported)")

    # --- Expert/HCI analysis: tshark -r btmon.btsnoop -q -z expert ---
    hci_result = _run_tshark(
        btsnoop, ["-q", "-z", "expert"], raw_dir / "hci-expert.txt",
    )
    if hci_result and hci_result.stdout:
        summary["hci_summary"] = _parse_hci_summary(hci_result.stdout)
    else:
        summary["errors"].append("hci-expert: tshark produced no output")

    # --- Frame sizes ---
    summary["frame_sizes"] = _compute_frame_sizes(raw_dir)

    # --- Write outputs ---
    _write_json(summary, run_dir / "tshark-summary.json")
    _write_markdown(summary, run_dir / "tshark-summary.md")
    log.info("wrote tshark-summary.json and tshark-summary.md")

    return summary
