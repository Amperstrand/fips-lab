from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from .config_gen import write_lab_acl, write_resolved_devices
from .device import Device, make_device
from .inventory import Inventory
from .results import copy_scenario, create_run_dir, now_iso, write_json
from .scenario import Scenario

log = logging.getLogger(__name__)


class LabRunner:
    def __init__(
        self,
        scenario: Scenario,
        inventory: Inventory,
        results_dir: Path,
        dry_run: bool = False,
        duration_override: int | None = None,
    ):
        self.scenario = scenario
        self.inventory = inventory
        self.results_dir = results_dir
        self.dry_run = dry_run
        self.duration_override = duration_override
        self.run_dir: Path | None = None
        self.devices: dict[str, Device] = {}
        self.resolved_configs: dict[str, dict[str, Any]] = {}

    def run(self) -> int:
        self.run_dir = create_run_dir(self.results_dir, self.scenario.name)
        self._setup_logging()
        log.info("Starting scenario %s", self.scenario.name)
        try:
            self._resolve_devices()
            self._write_static_artifacts()
            self._setup_isolation()
            self._collect_initial_snapshots()
            if not self.dry_run:
                self._test_loop()
            self._collect_final_snapshots()
            self._write_analysis("pass")
            return 0
        except Exception as exc:
            log.exception("Scenario failed: %s", exc)
            self._write_analysis("error", error=str(exc))
            return 1

    def _setup_logging(self) -> None:
        assert self.run_dir is not None
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S")
        file_handler = logging.FileHandler(self.run_dir / "runner.log")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(file_handler)

    def _resolve_devices(self) -> None:
        for topology_entry in self.scenario.topology_devices:
            alias = str(topology_entry["id"])
            inventory_ref = str(topology_entry["inventory_ref"])
            config = dict(self.inventory.device(inventory_ref))
            config["inventory_ref"] = inventory_ref
            config["scenario_role"] = topology_entry.get("role")
            self.resolved_configs[alias] = config
            self.devices[alias] = make_device(alias, config, dry_run=self.dry_run)
        log.info("Resolved %d devices: %s", len(self.devices), ", ".join(self.devices))

    def _write_static_artifacts(self) -> None:
        assert self.run_dir is not None
        copy_scenario(self.scenario.path, self.run_dir)
        write_resolved_devices(self.run_dir, self.resolved_configs)
        metadata = {
            "timestamp": now_iso(),
            "scenario": self.scenario.name,
            "duration_secs": self.duration_override or self.scenario.duration_secs,
            "dry_run": self.dry_run,
            "inventory_path": str(self.inventory.path),
            "lab": self.inventory.lab,
            "git": _git_metadata(Path.cwd()),
            "devices": {
                alias: {
                    "inventory_ref": config.get("inventory_ref"),
                    "type": config.get("type"),
                    "platform": config.get("platform"),
                    "transport": config.get("transport"),
                    "identity": config.get("identity"),
                }
                for alias, config in self.resolved_configs.items()
            },
        }
        write_json(self.run_dir / "metadata.json", metadata)

    def _setup_isolation(self) -> None:
        assert self.run_dir is not None
        isolation = self.scenario.raw.get("isolation") or {}
        if not isolation:
            return
        acl = write_lab_acl(self.run_dir, self.resolved_configs, isolation)
        write_json(self.run_dir / "isolation.json", acl)
        if acl["missing_identities"]:
            log.warning("Lab allowlist incomplete: %s", "; ".join(acl["missing_identities"]))

    def _collect_initial_snapshots(self) -> None:
        self._collect_snapshots("initial")

    def _collect_final_snapshots(self) -> None:
        self._collect_snapshots("final")

    def _collect_snapshots(self, label: str) -> None:
        assert self.run_dir is not None
        commands = self.scenario.metrics.get("collect") or ["show_status", "show_peers", "show_mmp"]
        from_aliases = self.scenario.metrics.get("from") or list(self.devices.keys())
        snapshot: dict[str, Any] = {}
        for alias in from_aliases:
            device = self.devices.get(alias)
            if not device or device.type != "fips":
                continue
            snapshot[alias] = {command: device.query(command) for command in commands}
        write_json(self.run_dir / f"snapshot-{label}.json", snapshot)

    def _test_loop(self) -> None:
        duration = self.duration_override or self.scenario.duration_secs
        interval = int(self.scenario.metrics.get("interval_secs") or 10)
        start = time.time()
        series: list[dict[str, Any]] = []
        while time.time() - start < duration:
            elapsed = int(time.time() - start)
            commands = self.scenario.metrics.get("collect") or ["show_status", "show_peers", "show_mmp"]
            from_aliases = self.scenario.metrics.get("from") or list(self.devices.keys())
            point: dict[str, Any] = {"t": elapsed, "devices": {}}
            for alias in from_aliases:
                device = self.devices.get(alias)
                if device and device.type == "fips":
                    point["devices"][alias] = {command: device.query(command) for command in commands}
            series.append(point)
            write_json(self.run_dir / "metrics-timeseries.json", series)
            time.sleep(interval)

    def _write_analysis(self, status: str, error: str | None = None) -> None:
        if self.run_dir is None:
            return
        lines = [f"scenario: {self.scenario.name}", f"status: {status}"]
        if error:
            lines.append(f"error: {error}")
        if self.dry_run:
            lines.append("dry_run: true")
        (self.run_dir / "analysis.txt").write_text("\n".join(lines) + "\n")


def _git_metadata(path: Path) -> dict[str, str | None]:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, timeout=5, check=False)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True, timeout=5, check=False)
        return {
            "commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "dirty": "yes" if dirty.stdout.strip() else "no",
        }
    except Exception:
        return {"commit": None, "dirty": None}
