"""ESP32 serial console driver — reset, capture, stats via SSH to serial-host."""

import json

import attr

from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.protocol.commandprotocol import CommandProtocol


@target_factory.reg_driver
@attr.s(eq=False)
class EspSerialDriver(Driver):
    """Control ESP32 via USB-serial on a remote host.

    Provides: raw read, hardware reset (DTR/RTS), stats query (show_stats JSON).
    All operations execute via SSH to the host that has the USB-serial connection.
    """

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

    @Driver.check_active
    def reset_and_capture(self, duration_secs=60):
        """Hardware reset ESP32 via DTR/RTS, then capture serial output."""
        script = (
            f"sudo python3 -c \""
            f"import serial,time;"
            f"s=serial.Serial('{self.serial_port}',{self.baud},timeout=0.1);"
            f"s.dtr=False;s.rts=True;time.sleep(0.1);"
            f"s.dtr=True;s.rts=True;time.sleep(0.05);"
            f"s.dtr=False;s.rts=False;time.sleep(0.2);"
            f"start=time.time();buf='';"
            f"while time.time()-start<{duration_secs}:"
            f"d=s.read(4096);buf+=d.decode(errors='replace') if d else '';"
            f"s.close();print(buf)"
            f"\""
        )
        result = self.shell.run_check(script)
        if isinstance(result, (list, tuple)):
            return "\n".join(
                r.decode("utf-8", errors="replace") if isinstance(r, bytes) else str(r)
                for r in result
            )
        return str(result)

    @Driver.check_active
    def send_command(self, command, timeout_secs=2):
        """Send a line command and read the response."""
        script = (
            f"sudo python3 -c \""
            f"import serial,time;"
            f"s=serial.Serial('{self.serial_port}',{self.baud},timeout=1);"
            f"s.write(b'{command}\\n');"
            f"time.sleep({timeout_secs});"
            f"d=s.read(4096);s.close();"
            f"print(d.decode(errors='replace'))"
            f"\""
        )
        result = self.shell.run_check(script)
        if isinstance(result, (list, tuple)):
            return "\n".join(
                r.decode("utf-8", errors="replace") if isinstance(r, bytes) else str(r)
                for r in result
            )
        return str(result)

    @Driver.check_active
    def show_stats(self):
        """Query show_stats and return parsed JSON data dict."""
        output = self.send_command("show_stats", timeout_secs=2)
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line).get("data", {})
                except Exception:
                    pass
        return {}
