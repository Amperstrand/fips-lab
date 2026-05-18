"""Driver for managing the fips system service (systemd / launchd)."""

import re
import time

import attr

from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.protocol.commandprotocol import CommandProtocol


@target_factory.reg_driver
@attr.s(eq=False)
class FipsServiceDriver(Driver):
    """Start / stop / restart the fips daemon via the system service manager.

    Supports ``systemd`` (Linux) and ``launchd`` (macOS).

    On Linux, the ``restart()`` method also resets the BLE adapter
    (``hciconfig hci0 down/up``) before restarting the service.  This
    works around a kernel bug where the HCI LE Create Connection opcode
    returns ``-EBUSY`` after prolonged scanning/connection cycling.
    """

    bindings = {"shell": "CommandProtocol"}

    service_kind = attr.ib(
        validator=attr.validators.in_(["systemd", "launchd"]),
    )
    config_path = attr.ib(validator=attr.validators.instance_of(str))
    service_name = attr.ib(
        default="",
        validator=attr.validators.instance_of(str),
    )
    label = attr.ib(
        default="",
        validator=attr.validators.instance_of(str),
    )
    ble_adapter = attr.ib(
        default="hci0",
        validator=attr.validators.instance_of(str),
    )

    def _systemd_cmd(self, action: str) -> str:
        return f"sudo systemctl {action} {self.service_name}"

    def _launchd_target(self) -> str:
        uid_expr = "$(id -u)"
        return f"gui/{uid_expr}/{self.label}"

    def _launchctl(self, *args: str) -> str:
        target = self._launchd_target()
        parts = " ".join(args)
        return f"sudo launchctl {parts} {target}"

    def _reset_ble_adapter(self) -> None:
        if self.service_kind != "systemd":
            return
        self.shell.run_check(f"sudo hciconfig {self.ble_adapter} down")
        time.sleep(2)
        self.shell.run_check(f"sudo hciconfig {self.ble_adapter} up")
        time.sleep(1)

    @Driver.check_active
    def start(self):
        if self.service_kind == "systemd":
            return self.shell.run_check(self._systemd_cmd("start"))
        return self.shell.run_check(self._launchctl("kickstart", "-k"))

    @Driver.check_active
    def stop(self):
        if self.service_kind == "systemd":
            return self.shell.run_check(self._systemd_cmd("stop"))
        return self.shell.run_check(self._launchctl("bootout"))

    @Driver.check_active
    def restart(self):
        self._reset_ble_adapter()
        if self.service_kind == "systemd":
            return self.shell.run_check(self._systemd_cmd("restart"))
        return self.shell.run_check(self._launchctl("kickstart", "-k"))

    @Driver.check_active
    def status(self) -> str:
        if self.service_kind == "systemd":
            output = self.shell.run_check(self._systemd_cmd("status"))
            if "active (running)" in output:
                return "running"
            if "inactive" in output:
                return "stopped"
            return "unknown"
        output = self.shell.run_check(
            f"sudo launchctl print {self._launchd_target()}"
        )
        if "state = running" in output:
            return "running"
        return "stopped"
