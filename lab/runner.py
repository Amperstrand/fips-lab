from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from .analysis import analyze_run, write_analysis as write_analysis_report
from .build import BuildManager
from .capture.btmon import BtmonCapture
from .capture.iperf import IperfSession
from .capture.keylog import KeylogCapture
from .capture.rssi import RssiCollector
from .capture.serial_log import SerialLogCapture
from .config_gen import write_lab_acl, write_resolved_devices
from .deploy import DeployManager
from .device import Device, make_device
from .inventory import Inventory
from .results import copy_scenario, create_run_dir, generate_charts, now_iso, write_json
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
        commit: str | None = None,
    ):
        self.scenario = scenario
        self.inventory = inventory
        self.results_dir = results_dir
        self.dry_run = dry_run
        self.duration_override = duration_override
        self.publish = publish
        self.commit = commit
        self.run_dir: Path | None = None
        self.devices: dict[str, Device] = {}
        self.resolved_configs: dict[str, dict[str, Any]] = {}
        self.captures: list[BtmonCapture | SerialLogCapture] = []
        self.rssi_collectors: list[RssiCollector] = []
        self.iperf: IperfSession | None = None
        self._defer_rssi: bool = False

    def run(self) -> int:
        self.run_dir = create_run_dir(self.results_dir, self.scenario.name)
        self._setup_logging()
        log.info("Starting scenario %s", self.scenario.name)
        try:
            self._resolve_devices()
            self._write_static_artifacts()
            self._setup_isolation()
            self._setup_captures()
            self._start_captures()
            self._deploy()
            self._collect_initial_snapshots()
            self._start_rssi_if_ready()
            if not self.dry_run:
                self._test_loop()
            self._collect_final_snapshots()
            self._stop_captures()
            self._collect_keylogs()
            self._collect_event_logs()
            self._run_iperf()
            self._run_btsnoop_decrypt()
            self._run_tshark_analysis()
            self._run_analysis()
            self._generate_charts()
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
            "lab": self.inventory.lab,
            "git": _git_metadata(Path.cwd()),
            "fips_git": _fips_git_metadata(self.resolved_configs),
            "microfips_git": _microfips_git_metadata(self.scenario, self.resolved_configs),
            "topology": {
                "devices": [
                    {
                        "id": d["id"],
                        "type": self.resolved_configs.get(str(d["id"]), {}).get("type"),
                        "platform": self.resolved_configs.get(str(d["id"]), {}).get("platform"),
                        "role": d.get("role"),
                    }
                    for d in self.scenario.topology_devices
                ],
                "links": self.scenario.raw.get("topology", {}).get("links", []),
            },
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
                    host=(device_cfg.get("host") or device_cfg.get("ssh_host", "")) if transport == "serial-via-ssh" else "",
                    user=(device_cfg.get("user") or device_cfg.get("ssh_user", "")) if transport == "serial-via-ssh" else "",
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
                    fipsctl_path=server_cfg.get("fipsctl", ""),
                )

        if capture_cfg.get("rssi"):
            self._defer_rssi = True

    def _start_captures(self) -> None:
        for cap in self.captures:
            cap.start()

    def _stop_captures(self) -> None:
        assert self.run_dir is not None
        capture_results: dict[str, Any] = {}
        for cap in self.captures:
            info = cap.stop()
            capture_results[info.get("device", cap.device_alias)] = info
        for rc in self.rssi_collectors:
            info = rc.stop()
            capture_results[f"rssi-{rc.device_alias}"] = info
        if capture_results:
            write_json(self.run_dir / "capture-results.json", capture_results)

    def _setup_rssi_collectors(self) -> None:
        assert self.run_dir is not None
        for alias, cfg in self.resolved_configs.items():
            if cfg.get("transport") != "ssh" or cfg.get("platform") != "linux":
                continue
            host = cfg.get("host", "")
            user = cfg.get("user", "")
            if not host:
                continue
            ble_addr = self._discover_ble_peer_addr(alias, cfg)
            if not ble_addr:
                log.warning("No BLE peer address found for RSSI collection on %s", alias)
                continue
            self.rssi_collectors.append(RssiCollector(
                device_alias=alias,
                host=host,
                user=user,
                ble_addr=ble_addr,
                results_dir=self.run_dir,
                enabled=not self.dry_run,
            ))

    def _start_rssi_if_ready(self) -> None:
        if not getattr(self, "_defer_rssi", False):
            return
        self._setup_rssi_collectors()
        for rc in self.rssi_collectors:
            rc.start()

    def _discover_ble_peer_addr(self, alias: str, cfg: dict[str, Any]) -> str:
        device = self.devices.get(alias)
        if not device or device.type != "fips":
            return ""
        peers_data = device.query("show_peers")
        if not peers_data or not isinstance(peers_data, dict):
            return ""
        for peer in peers_data.get("peers", []):
            if peer.get("transport_type") != "ble":
                continue
            addr = peer.get("ble_addr") or peer.get("address", "")
            if not addr and "/" in peer.get("transport_addr", ""):
                addr = peer["transport_addr"].split("/", 1)[1]
            if addr and ":" in addr:
                return str(addr)
        return ""

    def _run_iperf(self) -> None:
        if not self.iperf or not self.iperf.enabled:
            return
        if self._is_tun_disabled():
            log.info("iperf3: TUN disabled, skipping throughput tests")
            assert self.run_dir is not None
            write_json(self.run_dir / "iperf3-results.json", {
                "enabled": True, "skipped": True, "reason": "TUN disabled",
                "sessions": [],
            })
            return
        assert self.run_dir is not None
        result = self.iperf.run()
        write_json(self.run_dir / "iperf3-results.json", result)

    def _is_tun_disabled(self) -> bool:
        for alias, cfg in self.resolved_configs.items():
            if cfg.get("platform") != "linux":
                continue
            device = self.devices.get(alias)
            if not device or device.type != "fips":
                continue
            status = device.query("show_status")
            if isinstance(status, dict) and status.get("tun_state") == "disabled":
                return True
        return False

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

    def _collect_event_logs(self) -> None:
        assert self.run_dir is not None
        for alias, cfg in self.resolved_configs.items():
            if cfg.get("type") != "fips" or not cfg.get("keylog_path"):
                continue
            event_log_path = str(
                Path(cfg["keylog_path"]).parent / f"fips-ble-events-{alias}.jsonl"
            )
            transport = cfg.get("transport", "local")
            content = self._read_event_log(event_log_path, cfg, transport)
            if content is not None:
                local_path = self.run_dir / f"ble-events-{alias}.jsonl"
                local_path.write_text(content)
                lines = content.strip().splitlines()
                log.info("event_log %s: %d events → %s", alias, len(lines), local_path)

    @staticmethod
    def _read_event_log(path: str, cfg: dict[str, Any], transport: str) -> str | None:
        try:
            if transport == "ssh":
                host = cfg.get("host", "")
                user = cfg.get("user", "")
                target = f"{user}@{host}" if user else host
                use_sudo = cfg.get("sudo", False)
                cat_cmd = f"sudo cat {path}" if use_sudo else f"cat {path}"
                result = subprocess.run(
                    ["ssh", target, cat_cmd],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                return result.stdout if result.returncode == 0 else None
            else:
                try:
                    return Path(path).read_text()
                except PermissionError:
                    result = subprocess.run(
                        ["sudo", "cat", path],
                        capture_output=True, text=True, timeout=10, check=False,
                    )
                    return result.stdout if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            return None

    def _run_analysis(self) -> None:
        if self.run_dir is None:
            return
        report = analyze_run(self.run_dir)
        write_analysis_report(report, self.run_dir)
        log.info("Analysis: verdict=%s", report.verdict)

    def _run_btsnoop_decrypt(self) -> None:
        """Post-test: decrypt btsnoop capture using keylog keys."""
        if self.run_dir is None:
            return
        try:
            from lab.capture.btsnoop_decrypt import decrypt_btsnoop_capture
            decrypt_btsnoop_capture(self.run_dir)
        except ImportError:
            log.info("cryptography package not available, skipping btsnoop decryption")
        except Exception as exc:
            log.warning("btsnoop decryption failed: %s", exc)

    def _run_tshark_analysis(self) -> None:
        """Post-test: run tshark BLE statistics on btsnoop capture."""
        if self.run_dir is None:
            return
        try:
            from lab.capture.tshark import run_tshark_analysis
            run_tshark_analysis(self.run_dir)
        except Exception as exc:
            log.warning("tshark analysis failed: %s", exc)

    def _write_fallback_analysis(self, error: str) -> None:
        if self.run_dir is None:
            return
        lines = [f"scenario: {self.scenario.name}", f"status: error", f"error: {error}"]
        if self.dry_run:
            lines.append("dry_run: true")
        (self.run_dir / "analysis.txt").write_text("\n".join(lines) + "\n")

    def _deploy(self) -> None:
        assert self.run_dir is not None

        expected_commit: str | None = None
        build_metadata: dict[str, Any] | None = None

        if self.commit:
            expected_commit, build_metadata = self._build()

        manager = DeployManager(self.devices, self.resolved_configs, self.run_dir)

        deploy_cfg = self.scenario.raw.get("deploy") or {}
        microfips_deploy = deploy_cfg.get("microfips") or {}

        if microfips_deploy.get("flash"):
            if self.dry_run:
                log.info("Dry run: would flash microfips devices")
            else:
                manager.flash_all_microfips()
                log.info("Waiting 5s for microfips boot after flash")
                time.sleep(5)

        has_stm32 = any(
            cfg.get("type") == "stm32-hil" for cfg in self.resolved_configs.values()
        )
        if has_stm32:
            if self.dry_run:
                log.info("Dry run: would run STM32 HIL tests")
            else:
                hil_results = manager.run_stm32_hil()
                write_json(self.run_dir / "stm32-hil-results.json", hil_results)

        if deploy_cfg.get("restart_before_test"):
            if self.dry_run:
                log.info("Dry run: would restart FIPS nodes")
            else:
                keylog = deploy_cfg.get("keylog", True)
                fips_meta = _fips_git_metadata(self.resolved_configs)
                expected_commit = fips_meta.get("commit")
                manager.restart_all(keylog=keylog, expected_commit=expected_commit)
                warmup = int(deploy_cfg.get("warmup_secs", 30))
                if warmup > 0:
                    log.info("Warmup: waiting %ds for BLE discovery", warmup)
                    time.sleep(warmup)
            self._start_rssi_if_ready()

        if build_metadata:
            self._write_build_metadata(build_metadata)

    def _build(self) -> tuple[str, dict[str, Any]]:
        """Run BuildManager to checkout+build on all devices.

        Returns (resolved_commit, build_metadata_dict).
        Raises RuntimeError on build failure.
        """
        assert self.run_dir is not None
        assert self.commit is not None
        log.info("Building FIPS commit %s on all devices", self.commit)
        build_mgr = BuildManager(self.devices, self.resolved_configs, self.run_dir)
        results = build_mgr.build_all(self.commit)

        failures = {alias: r for alias, r in results.items() if not r.success}
        if failures:
            msgs = [f"  {alias}: {r.error}" for alias, r in failures.items()]
            raise RuntimeError(
                f"Build failed on {len(failures)} device(s):\n" + "\n".join(msgs)
            )

        resolved_commits = {r.commit for r in results.values()}
        if len(resolved_commits) == 1:
            resolved = resolved_commits.pop()
        else:
            log.warning("Devices resolved to different commits: %s", resolved_commits)
            resolved = next(iter(resolved_commits))

        build_metadata: dict[str, Any] = {
            "requested_commit": self.commit,
            "resolved_commits": {alias: r.commit for alias, r in results.items()},
            "build_results": {
                alias: {"success": r.success, "duration_secs": round(r.duration_secs, 1)}
                for alias, r in results.items()
            },
        }
        return resolved, build_metadata

    def _write_build_metadata(self, build_metadata: dict[str, Any]) -> None:
        assert self.run_dir is not None
        metadata_path = self.run_dir / "metadata.json"
        if not metadata_path.exists():
            return
        import json
        try:
            existing = json.loads(metadata_path.read_text())
            existing["build"] = build_metadata
            metadata_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to update metadata with build info: %s", exc)

    def _generate_charts(self) -> None:
        if self.run_dir is None:
            return
        generate_charts(self.run_dir)

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


def _fips_git_metadata(resolved_configs: dict[str, dict[str, Any]]) -> dict[str, str | None]:
    """Extract git metadata from the FIPS source repo.

    Looks for the FIPS repo path from device configs:
    1. ``repo_path`` field (e.g. Linux remote)
    2. ``fips_binary`` field — strips ``/target/release/fips`` to find the repo root
    3. Falls back to common sibling directory ``../fips`` relative to fips-lab

    Only local paths are queried (not SSH remotes).
    """
    fips_path: Path | None = None
    for config in resolved_configs.values():
        transport = config.get("transport", "")
        if transport in ("ssh", "serial-via-ssh"):
            continue
        repo_path = config.get("repo_path")
        if repo_path:
            candidate = Path(repo_path)
            if (candidate / ".git").exists():
                fips_path = candidate
                break
        fips_binary = config.get("fips_binary")
        if fips_binary:
            candidate = Path(fips_binary).parent.parent.parent
            if (candidate / ".git").exists():
                fips_path = candidate
                break

    if fips_path is None:
        sibling = Path(__file__).resolve().parent.parent.parent / "fips"
        if (sibling / ".git").exists():
            fips_path = sibling

    if fips_path is None:
        return {"commit": None, "branch": None, "dirty": None}

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fips_path,
            capture_output=True, text=True, timeout=5, check=False,
        )
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=fips_path,
            capture_output=True, text=True, timeout=5, check=False,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=fips_path,
            capture_output=True, text=True, timeout=5, check=False,
        )
        return {
            "commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "branch": branch.stdout.strip() if branch.returncode == 0 else None,
            "dirty": "yes" if dirty.stdout.strip() else "no",
        }
    except Exception:
        return {"commit": None, "branch": None, "dirty": None}


def _microfips_git_metadata(
    scenario: Scenario,
    resolved_configs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Extract git metadata from the microfips firmware repo.

    Looks for a local microfips checkout:
    1. ``scenario.raw["artifacts"]["microfips"]`` for repo path info
    2. Any device config with ``type: microfips`` that has a ``repo_path``
    3. Falls back to sibling directory ``../microfips`` relative to fips-lab

    Also extracts the microfips mode from scenario topology (transport type
    on links to microfips devices; defaults to "ble").
    """
    microfips_cfg = scenario.raw.get("artifacts", {}).get("microfips", {})
    if not microfips_cfg:
        has_microfips_device = any(
            c.get("type") == "microfips" for c in resolved_configs.values()
        )
        if not has_microfips_device:
            return {}

    mode = "ble"
    microfips_ids = {
        alias for alias, cfg in resolved_configs.items() if cfg.get("type") == "microfips"
    }
    links = scenario.raw.get("topology", {}).get("links", [])
    for link in links:
        if link.get("to") in microfips_ids or link.get("from") in microfips_ids:
            mode = link.get("transport", "ble")
            break

    repo_path: Path | None = None

    for config in resolved_configs.values():
        if config.get("type") != "microfips":
            continue
        rp = config.get("repo_path")
        if rp:
            candidate = Path(rp)
            if (candidate / ".git").exists():
                repo_path = candidate
                break

    if repo_path is None:
        sibling = Path(__file__).resolve().parent.parent.parent / "microfips"
        if (sibling / ".git").exists():
            repo_path = sibling

    result: dict[str, Any] = {
        "mode": mode,
        "repo": microfips_cfg.get("repo", ""),
    }

    if repo_path is None:
        result.update({"commit": None, "branch": None, "dirty": None})
        return result

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path,
            capture_output=True, text=True, timeout=5, check=False,
        )
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path,
            capture_output=True, text=True, timeout=5, check=False,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo_path,
            capture_output=True, text=True, timeout=5, check=False,
        )
        result.update({
            "commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "branch": branch.stdout.strip() if branch.returncode == 0 else None,
            "dirty": "yes" if dirty.stdout.strip() else "no",
        })
    except Exception:
        result.update({"commit": None, "branch": None, "dirty": None})

    return result
