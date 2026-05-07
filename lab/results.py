from __future__ import annotations

import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def create_run_dir(base: Path, scenario_name: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = base / f"{timestamp}-{scenario_name}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def copy_scenario(src: Path, dst_dir: Path) -> None:
    shutil.copy2(src, dst_dir / "scenario.yaml")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


# ── SVG chart generation ──────────────────────────────────────────────────

_MARGINS = {"left": 70, "right": 20, "top": 30, "bottom": 45}
_WIDTH = 820
_HEIGHT = 300
_COLORS = ["#1f4068", "#e07020", "#2e8b57", "#8b2252", "#6a5acd"]

_SERIES_COLORS = {
    "rtt": "#1f4068",
    "peers": "#2e8b57",
    "rekey": "#6a5acd",
    "disconnect": "#d93025",
}


def _nice_ticks(lo: float, hi: float, max_ticks: int = 6) -> list[float]:
    span = hi - lo if hi > lo else 1.0
    raw_step = span / max_ticks
    mag = 10 ** math.floor(math.log10(raw_step)) if raw_step > 0 else 1
    for step in (mag, 2 * mag, 5 * mag, 10 * mag):
        if span / step <= max_ticks:
            break
    start = math.floor(lo / step) * step
    ticks: list[float] = []
    v = start
    while v <= hi + step * 0.01:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _svg_header(title: str) -> str:
    ml, mr, mt, mb = _MARGINS["left"], _MARGINS["right"], _MARGINS["top"], _MARGINS["bottom"]
    cw = _WIDTH - ml - mr
    ch = _HEIGHT - mt - mb
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" '
        f'viewBox="0 0 {_WIDTH} {_HEIGHT}" font-family="-apple-system,BlinkMacSystemFont,'
        f'sans-serif">\n'
        f'<rect width="{_WIDTH}" height="{_HEIGHT}" fill="#fff"/>\n'
        f'<text x="{ml + cw / 2}" y="18" text-anchor="middle" font-size="13" '
        f'font-weight="600" fill="#1a1a2e">{title}</text>\n'
    )


def _svg_axes(
    x_lo: float, x_hi: float, y_lo: float, y_hi: float,
    x_label: str, y_label: str, x_fmt: str = "{}",
) -> str:
    ml, mr, mt, mb = _MARGINS["left"], _MARGINS["right"], _MARGINS["top"], _MARGINS["bottom"]
    cw = _WIDTH - ml - mr
    ch = _HEIGHT - mt - mb
    parts: list[str] = []

    parts.append(
        f'<rect x="{ml}" y="{mt}" width="{cw}" height="{ch}" '
        f'fill="none" stroke="#ddd" stroke-width="1"/>'
    )

    y_ticks = _nice_ticks(y_lo, y_hi)
    for v in y_ticks:
        frac = (v - y_lo) / (y_hi - y_lo) if y_hi > y_lo else 0.0
        py = mt + ch - frac * ch
        if py < mt or py > mt + ch:
            continue
        parts.append(
            f'<line x1="{ml}" y1="{py:.1f}" x2="{ml + cw}" y2="{py:.1f}" '
            f'stroke="#eee" stroke-width="1"/>'
        )
        label_val = f"{v:.0f}" if abs(v) >= 1 else f"{v:.2f}"
        parts.append(
            f'<text x="{ml - 6}" y="{py:.1f}" text-anchor="end" '
            f'dominant-baseline="middle" font-size="10" fill="#666">'
            f'{label_val}</text>'
        )

    x_ticks = _nice_ticks(x_lo, x_hi)
    for v in x_ticks:
        frac = (v - x_lo) / (x_hi - x_lo) if x_hi > x_lo else 0.0
        px = ml + frac * cw
        if px < ml or px > ml + cw:
            continue
        parts.append(
            f'<line x1="{px:.1f}" y1="{mt}" x2="{px:.1f}" y2="{mt + ch}" '
            f'stroke="#eee" stroke-width="1"/>'
        )
        label = x_fmt.format(v)
        parts.append(
            f'<text x="{px:.1f}" y="{mt + ch + 16}" text-anchor="middle" '
            f'font-size="10" fill="#666">{label}</text>'
        )

    parts.append(
        f'<text x="{ml + cw / 2}" y="{_HEIGHT - 4}" text-anchor="middle" '
        f'font-size="11" fill="#444">{x_label}</text>'
    )
    parts.append(
        f'<text x="14" y="{mt + ch / 2}" text-anchor="middle" '
        f'font-size="11" fill="#444" transform="rotate(-90,14,{mt + ch / 2})">'
        f'{y_label}</text>'
    )
    return "\n".join(parts)


def _polyline(
    points: list[tuple[float, float]],
    x_lo: float, x_hi: float, y_lo: float, y_hi: float,
    color: str, label: str,
) -> str:
    ml, mt = _MARGINS["left"], _MARGINS["top"]
    cw = _WIDTH - ml - _MARGINS["right"]
    ch = _HEIGHT - mt - _MARGINS["bottom"]
    x_span = x_hi - x_lo if x_hi > x_lo else 1.0
    y_span = y_hi - y_lo if y_hi > y_lo else 1.0

    coords: list[str] = []
    for x, y in points:
        px = ml + (x - x_lo) / x_span * cw
        py = mt + ch - (y - y_lo) / y_span * ch
        coords.append(f"{px:.1f},{py:.1f}")

    if not coords:
        return ""

    return (
        f'<polyline points="{" ".join(coords)}" fill="none" '
        f'stroke="{color}" stroke-width="1.5" stroke-linejoin="round"/>'
    )


def _extract_rtt_series(timeseries: list[dict[str, Any]]) -> dict[str, list[tuple[float, float]]]:
    series: dict[str, list[tuple[float, float]]] = {}
    for point in timeseries:
        t = point.get("t", 0)
        for alias, dev in point.get("devices", {}).items():
            for peer in dev.get("show_mmp", {}).get("peers", []):
                ll = peer.get("link_layer", {})
                srtt = ll.get("srtt_ms")
                if srtt is not None:
                    peer_short = peer.get("display_name", "?")
                    key = f"{alias} → {peer_short}"
                    series.setdefault(key, []).append((t, srtt))
    return series


def _extract_peer_count_series(timeseries: list[dict[str, Any]]) -> dict[str, list[tuple[float, float]]]:
    series: dict[str, list[tuple[float, float]]] = {}
    for point in timeseries:
        t = point.get("t", 0)
        for alias, dev in point.get("devices", {}).items():
            status = dev.get("show_status", {})
            pc = status.get("peer_count")
            if pc is not None:
                series.setdefault(alias, []).append((t, float(pc)))
    return series


def generate_chart_rtt(run_dir: Path, timeseries: list[dict[str, Any]]) -> None:
    all_series = _extract_rtt_series(timeseries)
    if not all_series:
        return

    all_x = [x for pts in all_series.values() for x, _ in pts]
    all_y = [y for pts in all_series.values() for _, y in pts]
    x_lo, x_hi = min(all_x), max(all_x)
    y_lo = 0
    y_hi = max(all_y) * 1.1 if all_y else 100

    svg = _svg_header("MMP RTT over Time")
    svg += _svg_axes(x_lo, x_hi, y_lo, y_hi, "Time (s)", "RTT (ms)", x_fmt="{:.0f}")

    for i, (label, points) in enumerate(all_series.items()):
        color = _COLORS[i % len(_COLORS)]
        svg += _polyline(points, x_lo, x_hi, y_lo, y_hi, color, label)

    ml, mt, mb = _MARGINS["left"], _MARGINS["top"], _MARGINS["bottom"]
    ch = _HEIGHT - mt - mb
    ly = mt + ch + 30
    lx = ml
    for i, label in enumerate(all_series):
        color = _COLORS[i % len(_COLORS)]
        svg += f'<rect x="{lx}" y="{ly - 8}" width="12" height="12" fill="{color}" rx="2"/>'
        svg += f'<text x="{lx + 16}" y="{ly + 2}" font-size="10" fill="#444">{label}</text>'
        lx += len(label) * 7 + 30

    svg += "\n</svg>"
    (run_dir / "chart-rtt.svg").write_text(svg)


def generate_chart_peers(run_dir: Path, timeseries: list[dict[str, Any]]) -> None:
    all_series = _extract_peer_count_series(timeseries)
    if not all_series:
        return

    all_x = [x for pts in all_series.values() for x, _ in pts]
    all_y = [y for pts in all_series.values() for _, y in pts]
    x_lo, x_hi = min(all_x), max(all_x)
    y_lo = 0
    y_hi = max(max(all_y) + 0.5, 2)

    svg = _svg_header("Peer Count over Time")
    svg += _svg_axes(x_lo, x_hi, y_lo, y_hi, "Time (s)", "Peers", x_fmt="{:.0f}")

    ml, mt, mr, mb = _MARGINS["left"], _MARGINS["top"], _MARGINS["right"], _MARGINS["bottom"]
    cw = _WIDTH - ml - mr
    ch = _HEIGHT - mt - mb

    for i, (label, points) in enumerate(all_series.items()):
        color = _COLORS[i % len(_COLORS)]
        x_span = x_hi - x_lo if x_hi > x_lo else 1.0
        y_span = y_hi - y_lo if y_hi > y_lo else 1.0
        step_pts: list[str] = []
        for j, (x, y) in enumerate(points):
            px = ml + (x - x_lo) / x_span * cw
            py = mt + ch - (y - y_lo) / y_span * ch
            step_pts.append(f"{px:.1f},{py:.1f}")
            if j + 1 < len(points):
                nx = points[j + 1][0]
                npx = ml + (nx - x_lo) / x_span * cw
                step_pts.append(f"{npx:.1f},{py:.1f}")
        if step_pts:
            svg += (
                f'<polyline points="{" ".join(step_pts)}" fill="none" '
                f'stroke="{color}" stroke-width="1.5"/>'
            )

    ly = mt + ch + 30
    lx = ml
    for i, label in enumerate(all_series):
        color = _COLORS[i % len(_COLORS)]
        svg += f'<rect x="{lx}" y="{ly - 8}" width="12" height="12" fill="{color}" rx="2"/>'
        svg += f'<text x="{lx + 16}" y="{ly + 2}" font-size="10" fill="#444">{label}</text>'
        lx += len(label) * 7 + 30

    svg += "\n</svg>"
    (run_dir / "chart-peers.svg").write_text(svg)


def generate_chart_rekeys(run_dir: Path, timeseries: list[dict[str, Any]]) -> None:
    analysis_path = run_dir / "analysis.json"
    if not analysis_path.exists():
        return

    with open(analysis_path) as f:
        analysis = json.load(f)

    all_x = [p.get("t", 0) for p in timeseries]
    if not all_x:
        return
    x_lo, x_hi = 0, max(all_x)
    x_span = x_hi - x_lo if x_hi > x_lo else 1.0

    rekey_stats = analysis.get("rekey_stats", [])
    disconnects = analysis.get("disconnects", [])

    total_rekeys = sum(s.get("total_rekeys", 0) for s in rekey_stats)
    if total_rekeys == 0 and not disconnects:
        return

    y_lo, y_hi = 0, 1
    ml, mt, mr, mb = _MARGINS["left"], _MARGINS["top"], _MARGINS["right"], _MARGINS["bottom"]
    cw = _WIDTH - ml - mr
    ch = _HEIGHT - mt - mb

    svg = _svg_header("Rekey Events &amp; Disconnects")
    svg += _svg_axes(x_lo, x_hi, y_lo, y_hi, "Time (s)", "", x_fmt="{:.0f}")

    for stat in rekey_stats:
        total = stat.get("total_rekeys", 0)
        pair = stat.get("pair", "?")
        if total <= 0 or x_hi <= 0:
            continue
        interval = x_hi / total
        for k in range(total):
            t = interval * (k + 1)
            if t > x_hi:
                break
            px = ml + (t - x_lo) / x_span * cw
            svg += (
                f'<line x1="{px:.1f}" y1="{mt}" x2="{px:.1f}" '
                f'y2="{mt + ch}" stroke="{_SERIES_COLORS["rekey"]}" '
                f'stroke-width="1" opacity="0.5"/>'
            )

    for disc in disconnects:
        t = disc.get("t", 0)
        pair = disc.get("pair", "?")
        reconnected = disc.get("reconnected", False)
        px = ml + (t - x_lo) / x_span * cw
        svg += (
            f'<line x1="{px:.1f}" y1="{mt}" x2="{px:.1f}" '
            f'y2="{mt + ch}" stroke="{_SERIES_COLORS["disconnect"]}" '
            f'stroke-width="2.5" opacity="0.9"/>'
        )
        marker_text = "R" if reconnected else "X"
        svg += (
            f'<text x="{px:.1f}" y="{mt - 4}" text-anchor="middle" '
            f'font-size="9" font-weight="700" fill="{_SERIES_COLORS["disconnect"]}">'
            f'{marker_text}</text>'
        )

    ly = mt + ch + 30
    lx = ml
    svg += (
        f'<rect x="{lx}" y="{ly - 8}" width="12" height="12" '
        f'fill="{_SERIES_COLORS["rekey"]}" opacity="0.5" rx="2"/>'
    )
    svg += f'<text x="{lx + 16}" y="{ly + 2}" font-size="10" fill="#444">Rekey ({total_rekeys})</text>'
    lx += 120
    if disconnects:
        svg += (
            f'<rect x="{lx}" y="{ly - 8}" width="12" height="12" '
            f'fill="{_SERIES_COLORS["disconnect"]}" rx="2"/>'
        )
        svg += (
            f'<text x="{lx + 16}" y="{ly + 2}" font-size="10" fill="#444">'
            f'Disconnect ({len(disconnects)})</text>'
        )

    svg += "\n</svg>"
    (run_dir / "chart-rekeys.svg").write_text(svg)


def generate_charts(run_dir: Path) -> None:
    ts_path = run_dir / "metrics-timeseries.json"
    if not ts_path.exists():
        return

    with open(ts_path) as f:
        timeseries = json.load(f)

    if not timeseries:
        return

    generate_chart_rtt(run_dir, timeseries)
    generate_chart_peers(run_dir, timeseries)
    generate_chart_rekeys(run_dir, timeseries)
