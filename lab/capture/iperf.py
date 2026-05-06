"""iperf3 throughput sessions over the FIPS mesh."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class IperfSession:
    """Run iperf3 TCP and UDP tests between FIPS mesh nodes.

    Starts iperf3 server on the target via SSH, runs client locally,
    saves JSON results.
    """

    enabled: bool = True
    server_host: str = ""
    server_user: str = ""
    server_ipv6: str = ""
    client_ipv6: str = ""
    results_dir: Path = Path(".")
    duration_tcp: int = 10
    duration_udp: int = 10
    udp_rate: str = "50K"
    tcp_window: str = "8K"

    def _ssh_target(self) -> str:
        if self.server_user:
            return f"{self.server_user}@{self.server_host}"
        return self.server_host

    def _ssh(self, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["ssh", self._ssh_target(), cmd],
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    def _resolve_server_ipv6(self) -> str:
        if self.server_ipv6:
            return self.server_ipv6
        # Try reading from fipsctl
        result = self._ssh("sudo fipsctl show status 2>/dev/null")
        if result.returncode == 0:
            try:
                status = json.loads(result.stdout)
                addr = status.get("address", "")
                if addr:
                    return addr
            except json.JSONDecodeError:
                pass
        # Fallback: read from fips0 interface
        result = self._ssh("ip -6 addr show fips0 scope global 2>/dev/null | grep -oP 'inet6 \\K[^/]+'")
        return result.stdout.strip()

    def _run_client(self, args: list[str], label: str, timeout: int = 60) -> dict[str, Any]:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False,
        )
        entry: dict[str, Any] = {"test": label, "exit_code": result.returncode}

        # Try parsing JSON output
        if result.stdout.strip():
            try:
                entry["data"] = json.loads(result.stdout)
            except json.JSONDecodeError:
                entry["output"] = result.stdout.strip()

        if result.returncode != 0:
            entry["error"] = result.stderr.strip()
            log.warning("iperf3 %s failed: %s", label, result.stderr.strip())
        else:
            log.info("iperf3 %s completed", label)

        return entry

    def run(self) -> dict[str, Any]:
        info: dict[str, Any] = {"enabled": self.enabled, "sessions": []}
        if not self.enabled or not self.server_host:
            return info

        server_ipv6 = self._resolve_server_ipv6()
        if not server_ipv6:
            log.warning("iperf3: cannot resolve server IPv6 on %s", self.server_host)
            info["error"] = "no server IPv6"
            return info

        info["server_ipv6"] = server_ipv6

        # Start iperf3 server on remote
        self._ssh("sudo killall -9 iperf3 2>/dev/null; iperf3 -s --daemon")
        time.sleep(2)

        client_bind = ["-B", self.client_ipv6] if self.client_ipv6 else []

        # TCP test
        tcp = self._run_client(
            ["iperf3", "-c", server_ipv6, *client_bind, "-t", str(self.duration_tcp),
             "-w", self.tcp_window, "-P", "1", "--json"],
            "tcp",
            timeout=self.duration_tcp + 30,
        )
        info["sessions"].append(tcp)

        # Save raw output
        out_path = self.results_dir / "iperf3-tcp.json"
        out_path.write_text(json.dumps(tcp, indent=2))

        time.sleep(2)

        # UDP test at BLE-appropriate rate
        udp = self._run_client(
            ["iperf3", "-c", server_ipv6, *client_bind, "-t", str(self.duration_udp),
             "-u", "-b", self.udp_rate, "-P", "1", "--json"],
            "udp",
            timeout=self.duration_udp + 30,
        )
        info["sessions"].append(udp)

        out_path = self.results_dir / "iperf3-udp.json"
        out_path.write_text(json.dumps(udp, indent=2))

        # Cleanup
        self._ssh("sudo killall -9 iperf3 2>/dev/null")

        return info
