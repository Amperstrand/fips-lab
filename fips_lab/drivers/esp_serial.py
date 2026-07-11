import attr

from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.protocol.commandprotocol import CommandProtocol


@target_factory.reg_driver
@attr.s(eq=False)
class EspSerialDriver(Driver):
    """Read ESP32 serial console output via SSH to the host with the USB-serial connection."""

    bindings = {"shell": "CommandProtocol"}

    serial_port = attr.ib(validator=attr.validators.instance_of(str))
    baud = attr.ib(default=115200, validator=attr.validators.instance_of(int))

    @Driver.check_active
    def read(self, duration_secs=10):
        cmd = (
            f"sudo stty -F {self.serial_port} {self.baud} raw -echo 2>/dev/null;"
            f" sudo timeout {duration_secs} cat {self.serial_port} 2>/dev/null || true"
        )
        result = self.shell.run_check(cmd)
        if isinstance(result, (list, tuple)):
            return "\n".join(
                r.decode("utf-8", errors="replace") if isinstance(r, bytes) else str(r)
                for r in result
            )
        return str(result)
