from __future__ import annotations

from typing import Any

from .device import Device


def snapshot(device: Device, commands: list[str]) -> dict[str, Any]:
    return {command: device.query(command) for command in commands}
