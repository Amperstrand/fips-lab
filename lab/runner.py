from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from .analysis import analyze_run, write_analysis as write_analysis_report
from .capture.btmon import BtmonCapture
from .capture.iperf import IperfSession
from .capture.keylog import KeylogCapture
from .capture.serial_log import SerialLogCapture
from .config_gen import write_lab_acl, write_resolved_devices
from .deploy import DeployManager
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
        publish: bool = False,
    ):
        self.scenario = scenario
        self.inventory = inventory
        self.results_dir = results_dir
        self.dry_run = dry_run
        self.duration_override = duration_override
        self.publish = publish
        self.run_dir: Path | None = None
        self.devices: dict[str, Device] = {}
        self.resolved_configs: dict[str, dict[str, Any]] = {}
        self.captures: list[BtmonCapture | SerialLogCapture] = []
        self.iperf: IperfSession | None = None

    def run(self) -> int:
        self.run_dir = create_run_dir(self.results_dir, self.scenario.name)
        self._setup_logging()
        log.info("Starting scenario %s", self.scenario.name)
        try:
            self._resolve_devices()
            self._write_static_artifacts()
            self._setup_isolation()
            self._deploy()
            self._setup_captures()
            self._start_captures()
            self._collect_initial_snapshots()
            if not self.dry_run:
                self._test_loop()
            self._collect_final_snapshots()
            self._stop_captures()
            self._collect_keylogs()
            self._run_iperf()
            self._run_analysis()
            self._deploy_cleanup()
            if self.publish:
                self._publish_results()
            return 0
        except Exception as exc:
            log.exception("Scenario failed: %s", exc)
            self._write_fallback_analysis(str(exc))
            self._deploy_cleanup()
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

    def _setup_captures(self) -> None:
        assert self.run_dir is not None
        capture_cfg = (self.scenario.raw.get("actions") or {}).get("capture") or {}
        if not capture_cfg:
            return

        for alias, device_cfg in self.resolved_configs.items():
            transport = device_cfg.get("transport", "")

            if capture_cfg.get("btmon") and transport == "ssh" and device_cfg.get("platform") == "linux":
                adapter = device_cfg.get("ble_adapter", "hci0")
                self.captures.append(BtmonCapture(
                    device_alias=alias,
                    host=device_cfg.get("host", ""),
                    user=device_cfg.get("user", ""),
                    adapter=adapter,
                    results_dir=self.run_dir,
                    enabled=not self.dry_run,
                ))

            if capture_cfg.get("serial") and transport in ("serial", "serial-via-ssh"):
                self.captures.append(SerialLogCapture(
                    device_alias=alias,
                    transport=transport,
                    host=device_cfg.get("host", "") if transport == "serial-via-ssh" else "",
                    user=device_cfg.get("user", "") if transport == "serial-via-ssh" else "",
                    serial_port=device_cfg.get("serial_port", ""),
                    baud_rate=device_cfg.get("baud_rate", 115200),
                    results_dir=self.run_dir,
                    enabled=not self.dry_run,
                ))

        if capture_cfg.get("iperf3"):
            server_cfg = next(
                (c for c in self.resolved_configs.values()
                 if c.get("transport") == "ssh" and c.get("platform") == "linux"),
                None,
            )
            if server_cfg:
                self.iperf = IperfSession(
                    enabled=not self.dry_run,
                    server_host=server_cfg.get("host", ""),
                    server_user=server_cfg.get("user", ""),
                    results_dir=self.run_dir,
                    duration_tcp=capture_cfg.get("iperf3_tcp_duration", 10),
                    duration_udp=capture_cfg.get("iperf3_udp_duration", 10),
                    udp_rate=capture_cfg.get("iperf3_udp_rate", "50K"),
                    tcp_window=capture_cfg.get("iperf3_tcp_window", "8K"),
                )

    def _start_captures(self) -> None:
        for cap in self.captures:
            cap.start()

    def _stop_captures(self) -> None:
        assert self.run_dir is not None
        capture_results: dict[str, Any] = {}
        for cap in self.captures:
            info = cap.stop()
            capture_results[info.get("device", cap.device_alias)] = info
        if capture_results:
            write_json(self.run_dir / "capture-results.json", capture_results)

    def _run_iperf(self) -> None:
        if self.iperf and self.iperf.enabled:
            assert self.run_dir is not None
            result = self.iperf.run()
            write_json(self.run_dir / "iperf3-results.json", result)

    def _collect_keylogs(self) -> None:
        assert self.run_dir is not None
        keylog_cfg = (self.scenario.raw.get("actions") or {}).get("capture") or {}
        if not keylog_cfg.get("keylog"):
            return
        devices_with_keylog: dict[str, dict[str, Any]] = {}
        for alias, cfg in self.resolved_configs.items():
            if cfg.get("type") == "fips" and cfg.get("keylog_path"):
                devices_with_keylog[alias] = cfg
        if not devices_with_keylog:
            return
        capture = KeylogCapture(results_dir=self.run_dir, devices=devices_with_keylog)
        result = capture.collect()
        write_json(self.run_dir / "keylog-results.json", result)

    def _run_analysis(self) -> None:
        if self.run_dir is None:
            return
        report = analyze_run(self.run_dir)
        write_analysis_report(report, self.run_dir)
        log.info("Analysis: verdict=%s", report.verdict)

    def _write_fallback_analysis(self, error: str) -> None:
        if self.run_dir is None:
            return
        lines = [f"scenario: {self.scenario.name}", f"status: error", f"error: {error}"]
        if self.dry_run:
            lines.append("dry_run: true")
        (self.run_dir / "analysis.txt").write_text("\n".join(lines) + "\n")

    def _deploy(self) -> None:
        deploy_cfg = self.scenario.raw.get("deploy") or {}
        if not deploy_cfg.get("restart_before_test"):
            return
        if self.dry_run:
            log.info("Dry run: would restart FIPS nodes")
            return
        assert self.run_dir is not None
        manager = DeployManager(self.devices, self.resolved_configs, self.run_dir)
        keylog = deploy_cfg.get("keylog", True)
        manager.restart_all(keylog=keylog)
        warmup = int(deploy_cfg.get("warmup_secs", 30))
        if warmup > 0:
            log.info("Warmup: waiting %ds for BLE discovery", warmup)
            time.sleep(warmup)

    def _deploy_cleanup(self) -> None:
        deploy_cfg = self.scenario.raw.get("deploy") or {}
        if not deploy_cfg.get("stop_after_test"):
            return
        if self.dry_run:
            return
        assert self.run_dir is not None
        manager = DeployManager(self.devices, self.resolved_configs, self.run_dir)
        manager.stop_all()

    def _publish_results(self) -> None:
        assert self.run_dir is not None
        repo_root = Path(__file__).resolve().parent.parent
        script_path = repo_root / "scripts" / "publish-report.sh"
        if not script_path.exists():
            log.warning("Publish script not found at %s", script_path)
            return
        log.info("Publishing results from %s", self.run_dir)
        result = subprocess.run(["bash", str(script_path), str(self.run_dir)], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            log.info("Publish succeeded")
        else:
            log.error("Publish failed: %s", result.stderr.strip() or result.stdout.strip())


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
