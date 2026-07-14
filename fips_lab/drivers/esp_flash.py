"""Driver for flashing firmware to ESP32 devices via esptool."""

import attr

from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.protocol.commandprotocol import CommandProtocol


@target_factory.reg_driver
@attr.s(eq=False)
class EspFlashDriver(Driver):
    """Flash firmware images to ESP32-family microcontrollers.

    Handles ELF-to-binary conversion and correct partition offset.
    Uses esptool over SSH to the host with the USB-serial connection.
    """

    bindings = {"shell": "CommandProtocol"}

    chip = attr.ib(validator=attr.validators.instance_of(str))
    serial_port = attr.ib(validator=attr.validators.instance_of(str))
    tool = attr.ib(default="esptool", validator=attr.validators.instance_of(str))
    baud = attr.ib(default=460800, validator=attr.validators.instance_of(int))
    flash_addr = attr.ib(default="0x10000", validator=attr.validators.instance_of(str))

    @Driver.check_active
    def flash(self, firmware_path: str):
        """Write firmware to the ESP32.

        Args:
            firmware_path: Path to ELF or binary on the remote host.
                          ELF files are auto-converted via elf2image.
        """
        if firmware_path.endswith(".elf") or "." not in firmware_path.rsplit("/", 1)[-1]:
            binary_path = "/tmp/fips-flash.bin"
            convert_cmd = (
                f"sudo {self.tool} --chip {self.chip} "
                f"elf2image {firmware_path} --output {binary_path}"
            )
            self.shell.run_check(convert_cmd)
            flash_path = binary_path
        else:
            flash_path = firmware_path

        cmd = (
            f"sudo {self.tool}"
            f" --chip {self.chip}"
            f" --port {self.serial_port}"
            f" --baud {self.baud}"
            f" --before default-reset"
            f" write-flash {self.flash_addr} {flash_path}"
        )
        return self.shell.run_check(cmd)

    @Driver.check_active
    def erase_flash(self):
        """Erase the entire flash chip."""
        return self.shell.run_check(
            f"sudo {self.tool} --chip {self.chip} --port {self.serial_port} erase-flash"
        )
