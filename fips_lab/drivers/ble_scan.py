import attr

from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.protocol.commandprotocol import CommandProtocol


@target_factory.reg_driver
@attr.s(eq=False)
class BLEScanDriver(Driver):
    """BLE LE scanning via hcitool on a remote host's Bluetooth adapter."""

    bindings = {"shell": "CommandProtocol"}

    adapter = attr.ib(default="hci0", validator=attr.validators.instance_of(str))

    @Driver.check_active
    def scan(self, duration_secs=10):
        cmd = (
            f"sudo timeout {duration_secs} hcitool -i {self.adapter} lescan 2>/dev/null"
            " || true"
        )
        result = self.shell.run_check(cmd)
        lines = []
        if isinstance(result, (list, tuple)):
            lines = [
                r.decode("utf-8", errors="replace") if isinstance(r, bytes) else str(r)
                for r in result
            ]
        elif isinstance(result, str):
            lines = result.splitlines()
        devices = {}
        for line in lines:
            line = line.strip()
            parts = line.split(" ", 1)
            if len(parts) == 2 and len(parts[0]) == 17:
                devices[parts[0]] = parts[1].strip()
        return devices

    @Driver.check_active
    def has_device(self, name=None, address=None, duration_secs=8):
        devices = self.scan(duration_secs)
        if name:
            return any(v == name for v in devices.values())
        if address:
            return address.lower() in (k.lower() for k in devices)
        return False
