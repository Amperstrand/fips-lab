"""Campaign runner — orchestrates multiple LabRunner scenarios sequentially."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .inventory import Inventory
from .results import now_iso, write_json
from .runner import LabRunner
from .scenario import Scenario

log = logging.getLogger(__name__)


class CampaignRunner:
    """Run multiple scenarios sequentially and produce a combined summary."""

    def __init__(
        self,
        campaign_path: str | Path,
        inventory: Inventory,
        results_dir: Path,
        dry_run: bool = False,
        publish: bool = False,
        commit: str | None = None,
    ):
        self.campaign_path = Path(campaign_path)
        self.inventory = inventory
        self.results_dir = results_dir
        self.dry_run = dry_run
        self.publish = publish
        self.commit = commit

        with self.campaign_path.open() as fh:
            raw = yaml.safe_load(fh) or {}

        campaign = raw.get("campaign") or {}
        self.name: str = str(campaign.get("name", self.campaign_path.stem))
        self.description: str = str(campaign.get("description", ""))
        self.scenario_paths: list[str] = [str(s) for s in (campaign.get("scenarios") or [])]

    def run(self) -> int:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        campaign_dir = self.results_dir / f"{timestamp}-{self.name}"
        campaign_dir.mkdir(parents=True, exist_ok=False)
        log.info("Campaign %s — results in %s", self.name, campaign_dir)

        scenario_results: list[_ScenarioResult] = []

        for scenario_path in self.scenario_paths:
            resolved = Path(scenario_path).resolve()
            if not resolved.exists():
                log.error("Scenario not found: %s", resolved)
                scenario_results.append(_ScenarioResult(
                    name=Path(scenario_path).stem,
                    path=str(scenario_path),
                    verdict="FAIL",
                    duration_secs=0,
                    rc=1,
                    error=f"Scenario file not found: {resolved}",
                ))
                continue

            scenario = Scenario.load(resolved)
            runner = LabRunner(
                scenario=scenario,
                inventory=self.inventory,
                results_dir=campaign_dir,
                dry_run=self.dry_run,
                publish=self.publish,
                commit=self.commit,
            )

            log.info("Campaign %s: running scenario %s", self.name, scenario.name)
            start = time.time()
            rc = runner.run()
            elapsed = int(time.time() - start)

            # Determine the run dir LabRunner created (latest matching dir)
            run_dir = _find_latest_run_dir(campaign_dir, scenario.name)

            # Load analysis if available
            verdict = "FAIL"
            assertions: list[dict] = []
            analysis_data: dict | None = None
            if run_dir:
                analysis_path = run_dir / "analysis.json"
                if analysis_path.exists():
                    try:
                        analysis_data = json.loads(analysis_path.read_text(encoding="utf-8"))
                        verdict = analysis_data.get("verdict", "FAIL") if analysis_data else "FAIL"
                        assertions = analysis_data.get("assertions", []) if analysis_data else []
                    except (json.JSONDecodeError, OSError):
                        pass

            scenario_results.append(_ScenarioResult(
                name=scenario.name,
                path=str(scenario_path),
                verdict=verdict if rc == 0 else "FAIL",
                duration_secs=elapsed,
                rc=rc,
                assertions=assertions,
                run_dir=run_dir,
                analysis=analysis_data,
            ))

            log.info(
                "Campaign %s: scenario %s → verdict=%s rc=%d",
                self.name, scenario.name,
                scenario_results[-1].verdict, rc,
            )

        # Generate campaign summary
        overall = _overall_verdict(scenario_results)
        _write_campaign_summary(campaign_dir, self.name, self.description, scenario_results, overall)

        log.info("Campaign %s complete — overall verdict: %s", self.name, overall)
        return 0 if overall == "PASS" else 1


class _ScenarioResult:
    """Internal tracker for a single scenario run within a campaign."""

    __slots__ = (
        "name", "path", "verdict", "duration_secs", "rc",
        "assertions", "run_dir", "analysis", "error",
    )

    def __init__(
        self,
        name: str,
        path: str,
        verdict: str,
        duration_secs: int,
        rc: int,
        assertions: list[dict] | None = None,
        run_dir: Path | None = None,
        analysis: dict | None = None,
        error: str | None = None,
    ):
        self.name = name
        self.path = path
        self.verdict = verdict
        self.duration_secs = duration_secs
        self.rc = rc
        self.assertions = assertions or []
        self.run_dir = run_dir
        self.analysis = analysis
        self.error = error


def _find_latest_run_dir(campaign_dir: Path, scenario_name: str) -> Path | None:
    """Find the most recently created run directory for *scenario_name* under *campaign_dir*."""
    candidates = sorted(
        campaign_dir.glob(f"*-{scenario_name}"),
        key=lambda p: p.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _overall_verdict(results: list[_ScenarioResult]) -> str:
    """Determine overall campaign verdict from individual scenario results."""
    if not results:
        return "FAIL"

    verdicts = {r.verdict for r in results}

    if "FAIL" in verdicts:
        return "FAIL"
    if "DEGRADED" in verdicts:
        return "DEGRADED"
    if verdicts == {"PASS"}:
        return "PASS"

    # If any are INSUFFICIENT_DATA or other, treat as FAIL
    return "PASS" if all(v == "PASS" for v in verdicts) else "FAIL"


def _write_campaign_summary(
    campaign_dir: Path,
    name: str,
    description: str,
    results: list[_ScenarioResult],
    overall: str,
) -> None:
    """Write campaign-summary.json and campaign-summary.md."""
    timestamp = now_iso()

    # Build JSON
    per_scenario: list[dict[str, Any]] = []
    for r in results:
        pass_count = sum(1 for a in r.assertions if a.get("passed"))
        fail_count = len(r.assertions) - pass_count
        per_scenario.append({
            "name": r.name,
            "path": r.path,
            "verdict": r.verdict,
            "duration_secs": r.duration_secs,
            "rc": r.rc,
            "assertions_pass": pass_count,
            "assertions_fail": fail_count,
            "error": r.error,
        })

    summary_json: dict[str, Any] = {
        "campaign_name": name,
        "description": description,
        "timestamp": timestamp,
        "overall_verdict": overall,
        "scenarios": per_scenario,
        "side_by_side": _build_side_by_side(results),
        "combined_assertions": _build_combined_assertions(results),
    }
    write_json(campaign_dir / "campaign-summary.json", summary_json)

    # Build markdown
    md = _format_campaign_markdown(name, description, timestamp, overall, results, summary_json)
    (campaign_dir / "campaign-summary.md").write_text(md, encoding="utf-8")
    log.info("Wrote campaign summary to %s", campaign_dir)


def _build_side_by_side(results: list[_ScenarioResult]) -> list[dict[str, Any]]:
    """Build side-by-side comparison from scenario analysis data."""
    comparisons: list[dict[str, Any]] = []
    for r in results:
        a = r.analysis or {}
        comparisons.append({
            "scenario": r.name,
            "connections": a.get("connections", []),
            "peer_metrics": a.get("peer_metrics", []),
            "rekey_stats": a.get("rekey_stats", []),
            "disconnects": a.get("disconnects", []),
        })
    return comparisons


def _build_combined_assertions(results: list[_ScenarioResult]) -> list[dict[str, Any]]:
    """Aggregate all assertions across scenarios."""
    combined: list[dict[str, Any]] = []
    for r in results:
        for a in r.assertions:
            combined.append({"scenario": r.name, **a})
    return combined


def _format_duration(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def _format_campaign_markdown(
    name: str,
    description: str,
    timestamp: str,
    overall: str,
    results: list[_ScenarioResult],
    summary_json: dict[str, Any],
) -> str:
    lines: list[str] = []

    verdict_icon = {"PASS": "✅", "FAIL": "❌", "DEGRADED": "⚠️"}.get(overall, "❓")
    total_duration = sum(r.duration_secs for r in results)

    lines.append(f"# Campaign Report — {name}")
    lines.append(
        f"**Date**: {timestamp} | **Total Duration**: {_format_duration(total_duration)}"
        f" | **Verdict**: {verdict_icon} {overall}"
    )
    if description:
        lines.append(f"\n> {description}")
    lines.append("")

    # Per-scenario summary table
    lines.append("## Scenario Summary")
    lines.append("| # | Scenario | Verdict | Duration | Assertions |")
    lines.append("|---|----------|---------|----------|------------|")
    for i, r in enumerate(results, 1):
        v_icon = {"PASS": "✅", "FAIL": "❌", "DEGRADED": "⚠️", "INSUFFICIENT_DATA": "❓"}.get(
            r.verdict, "❓"
        )
        pass_count = sum(1 for a in r.assertions if a.get("passed"))
        fail_count = len(r.assertions) - pass_count
        assert_str = f"{pass_count}✅ {fail_count}❌" if r.assertions else "N/A"
        lines.append(f"| {i} | {r.name} | {v_icon} {r.verdict} | {_format_duration(r.duration_secs)} | {assert_str} |")
    lines.append("")

    # Side-by-side comparison (only when 2 scenarios)
    comparisons = summary_json.get("side_by_side", [])
    if len(comparisons) == 2:
        s1, s2 = comparisons[0], comparisons[1]
        n1, n2 = s1["scenario"], s2["scenario"]

        # Connections comparison
        lines.append("## Side-by-Side Comparison")
        lines.append("### Connections")
        lines.append(f"| Metric | {n1} | {n2} |")
        lines.append("|--------|------|------|")
        c1 = s1.get("connections", [])
        c2 = s2.get("connections", [])
        all_pairs = sorted({c.get("pair", "") for c in c1 + c2})
        for pair in all_pairs:
            conn1 = next((c for c in c1 if c.get("pair") == pair), {})
            conn2 = next((c for c in c2 if c.get("pair") == pair), {})
            status1 = "✅ connected" if conn1.get("connected") else "❌ disconnected"
            status2 = "✅ connected" if conn2.get("connected") else "❌ disconnected"
            lines.append(f"| {pair} | {status1} | {status2} |")
        lines.append("")

        # MMP loss comparison
        pm1 = s1.get("peer_metrics", [])
        pm2 = s2.get("peer_metrics", [])
        all_pm_pairs = sorted({p.get("pair", "") for p in pm1 + pm2})
        if pm1 or pm2:
            lines.append("### MMP Loss")
            lines.append(f"| Pair | {n1} | {n2} |")
            lines.append("|------|------|------|")
            for pair in all_pm_pairs:
                m1 = next((p for p in pm1 if p.get("pair") == pair), {})
                m2 = next((p for p in pm2 if p.get("pair") == pair), {})
                loss1 = f"{m1.get('loss_avg', 0) * 100:.1f}% avg" if m1 else "N/A"
                loss2 = f"{m2.get('loss_avg', 0) * 100:.1f}% avg" if m2 else "N/A"
                lines.append(f"| {pair} | {loss1} | {loss2} |")
            lines.append("")

        if pm1 or pm2:
            lines.append("### RTT")
            lines.append(f"| Pair | {n1} | {n2} |")
            lines.append("|------|------|------|")
            for pair in all_pm_pairs:
                m1 = next((p for p in pm1 if p.get("pair") == pair), {})
                m2 = next((p for p in pm2 if p.get("pair") == pair), {})
                rtt1 = f"{m1.get('rtt_avg', 0):.0f}ms avg" if m1.get("rtt_avg") is not None else "N/A"
                rtt2 = f"{m2.get('rtt_avg', 0):.0f}ms avg" if m2.get("rtt_avg") is not None else "N/A"
                lines.append(f"| {pair} | {rtt1} | {rtt2} |")
            lines.append("")

        # Rekey stats comparison
        rk1 = s1.get("rekey_stats", [])
        rk2 = s2.get("rekey_stats", [])
        if rk1 or rk2:
            lines.append("### Rekey Stats")
            lines.append(f"| Pair | {n1} | {n2} |")
            lines.append("|------|------|------|")
            all_rk_pairs = sorted({r.get("pair", "") for r in rk1 + rk2})
            for pair in all_rk_pairs:
                r1 = next((r for r in rk1 if r.get("pair") == pair), {})
                r2 = next((r for r in rk2 if r.get("pair") == pair), {})
                rekey1 = f"{r1.get('total_rekeys', 0)} rekeys" if r1 else "N/A"
                rekey2 = f"{r2.get('total_rekeys', 0)} rekeys" if r2 else "N/A"
                lines.append(f"| {pair} | {rekey1} | {rekey2} |")
            lines.append("")

        # Disconnects comparison
        d1 = s1.get("disconnects", [])
        d2 = s2.get("disconnects", [])
        lines.append("### Disconnects")
        lines.append(f"| Metric | {n1} | {n2} |")
        lines.append("|--------|------|------|")
        lines.append(f"| Total disconnects | {len(d1)} | {len(d2)} |")
        recon1 = sum(1 for d in d1 if d.get("reconnected"))
        recon2 = sum(1 for d in d2 if d.get("reconnected"))
        lines.append(f"| Reconnected | {recon1} | {recon2} |")
        lines.append("")

    # Combined assertion summary
    combined = summary_json.get("combined_assertions", [])
    if combined:
        lines.append("## Combined Assertions")
        lines.append("| Scenario | Check | Expected | Actual | Result |")
        lines.append("|----------|-------|----------|--------|--------|")
        for a in combined:
            tag = "✅ PASS" if a.get("passed") else "❌ FAIL"
            lines.append(
                f"| {a.get('scenario', '')} | {a.get('name', '')} "
                f"| {a.get('expected', '')} | {a.get('actual', '')} | {tag} |"
            )
        lines.append("")

    return "\n".join(lines)
