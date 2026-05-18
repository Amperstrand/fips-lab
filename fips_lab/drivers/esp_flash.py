"""Driver for flashing firmware to ESP32 devices via esptool."""

import attr

from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.protocol.commandprotocol import CommandProtocol


@target_factory.reg_driver
@attr.s(eq=False)
class EspFlashDriver(Driver):
    """Flash firmware images to ESP32-family microcontrollers.

    Uses ``esptool.py`` over a serial port, typically reached via an
    SSH-bound shell driver (the ESP32 is connected to a remote host).
    """

    bindings = {"shell": "CommandProtocol"}

    chip = attr.ib(validator=attr.validators.instance_of(str))
    serial_port = attr.ib(validator=attr.validators.instance_of(str))
    tool = attr.ib(
        default="esptool.py",
        validator=attr.validators.instance_of(str),
    )
    baud = attr.ib(
        default=921600,
        validator=attr.validators.instance_of(int),
    )

    @Driver.check_active
    def flash(self, firmware_path: str):
        """Write *firmware_path* to the ESP32 at address 0x0.

        Assumes the firmware file is already present on the remote host.
        """
        cmd = (
            f"sudo {self.tool}"
            f" --chip {self.chip}"
            f" --port {self.serial_port}"
            f" --baud {self.baud}"
            f" write_flash 0x0 {firmware_path}"
        )
        return self.shell.run_check(cmd)
