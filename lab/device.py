from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


SHOW_COMMANDS = {
    "show_status": ["show", "status"],
    "show_peers": ["show", "peers"],
    "show_links": ["show", "links"],
    "show_tree": ["show", "tree"],
    "show_sessions": ["show", "sessions"],
    "show_bloom": ["show", "bloom"],
    "show_mmp": ["show", "mmp"],
    "show_cache": ["show", "cache"],
    "show_connections": ["show", "connections"],
    "show_transports": ["show", "transports"],
    "show_routing": ["show", "routing"],
    "show_identity_cache": ["show", "identity-cache"],
    "show_acl": ["acl", "show"],
}


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Device:
    def __init__(self, alias: str, device_id: str, config: dict[str, Any], dry_run: bool = False):
        self.alias = alias
        self.device_id = device_id
        self.config = config
        self.dry_run = dry_run

    @property
    def type(self) -> str:
        return str(self.config.get("type", "unknown"))

    @property
    def platform(self) -> str:
        return str(self.config.get("platform", "unknown"))

    def run(self, argv: list[str], timeout: int = 30) -> CommandResult:
        raise NotImplementedError

    def query(self, command: str) -> dict[str, Any] | None:
        if self.type != "fips":
            return None
        fipsctl = self.config.get("fipsctl", "fipsctl")
        socket = self.config.get("control_socket")
        parts = SHOW_COMMANDS.get(command, command.replace("_", " ").split())
        argv = [str(fipsctl)]
        if socket:
            argv.extend(["--socket", str(socket)])
        argv.extend(parts)
        result = self.run(argv, timeout=15)
        if result.returncode != 0:
            return {"error": result.stderr.strip() or result.stdout.strip(), "command": command}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"raw": result.stdout, "command": command}

    def identity_npub(self) -> str | None:
        identity = self.config.get("identity") or {}
        npub = identity.get("npub")
        return str(npub) if npub else None


class LocalDevice(Device):
    def run(self, argv: list[str], timeout: int = 30) -> CommandResult:
        if self.dry_run:
            return CommandResult(0, json.dumps({"dry_run": True, "argv": argv}), "")
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


class SshDevice(Device):
    def run(self, argv: list[str], timeout: int = 30) -> CommandResult:
        host = self.config.get("host") or self.config.get("ssh_host")
        user = self.config.get("user") or self.config.get("ssh_user")
        target = f"{user}@{host}" if user else str(host)
        use_sudo = self.config.get("sudo", False)
        remote = " ".join(_shell_quote(part) for part in argv)
        if use_sudo:
            remote = f"sudo {remote}"
        ssh_argv = ["ssh", target, remote]
        if self.dry_run:
            return CommandResult(0, json.dumps({"dry_run": True, "argv": ssh_argv}), "")
        proc = subprocess.run(ssh_argv, capture_output=True, text=True, timeout=timeout, check=False)
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


class SerialDevice(Device):
    def run(self, argv: list[str], timeout: int = 30) -> CommandResult:
        return CommandResult(1, "", f"serial device {self.device_id} cannot run shell commands: {argv}")


def make_device(alias: str, entry: dict[str, Any], dry_run: bool = False) -> Device:
    device_id = str(entry.get("inventory_ref") or alias)
    transport = str(entry.get("transport", "local"))
    if transport == "local":
        return LocalDevice(alias, device_id, entry, dry_run=dry_run)
    if transport == "ssh":
        return SshDevice(alias, device_id, entry, dry_run=dry_run)
    if transport in {"serial", "serial-via-ssh"}:
        return SerialDevice(alias, device_id, entry, dry_run=dry_run)
    raise ValueError(f"unsupported transport for {device_id}: {transport}")


def _shell_quote(value: object) -> str:
    text = str(value)
    return "'" + text.replace("'", "'\\''") + "'"
