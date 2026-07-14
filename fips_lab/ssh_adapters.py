"""SSH-based device adapters — standalone fallback for when labgrid is unavailable.

Same interface as the labgrid drivers (EspSerialDriver, FipsServiceDriver).
Allows tests to run with `pytest tests/test_esp32_l2cap.py` (no --lg-env)
or with `pytest --lg-env=environment.yaml tests/test_esp32_l2cap.py`.

Other projects (e.g. PRTA) can import these adapters for their own SSH-based
device control without depending on labgrid.
"""

import json
import subprocess
import time


def _ssh_run(host, cmd, timeout=30):
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"ubuntu@{host}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout.strip()


class SSHEsp32Adapter:
    """ESP32 serial control via SSH — same interface as EspSerialDriver."""

    def __init__(self, host="ai-legion", serial_port="/dev/ttyUSB0", baud=115200):
        self._host = host
        self.serial_port = serial_port
        self.baud = baud

    def read(self, duration_secs=10):
        cmd = (
            f"sudo stty -F {self.serial_port} {self.baud} raw -echo 2>/dev/null;"
            f" sudo timeout {duration_secs} cat {self.serial_port} 2>/dev/null || true"
        )
        return _ssh_run(self._host, cmd, timeout=duration_secs + 10)

    def reset_and_capture(self, duration_secs=60):
        script = (
            f'sudo python3 -c "'
            f"import serial,time;"
            f"s=serial.Serial('{self.serial_port}',{self.baud},timeout=0.1);"
            f"s.dtr=False;s.rts=True;time.sleep(0.1);"
            f"s.dtr=True;s.rts=True;time.sleep(0.05);"
            f"s.dtr=False;s.rts=False;time.sleep(0.2);"
            f"start=time.time();buf='';"
            f"while time.time()-start<{duration_secs}:"
            f"d=s.read(4096);buf+=d.decode(errors='replace') if d else '';"
            f"s.close();print(buf)"
            f'"'
        )
        return _ssh_run(self._host, script, timeout=duration_secs + 15)

    def send_command(self, command, timeout_secs=2):
        script = (
            f'sudo python3 -c "'
            f"import serial,time;"
            f"s=serial.Serial('{self.serial_port}',{self.baud},timeout=1);"
            f"s.write(b'{command}\\n');"
            f"time.sleep({timeout_secs});"
            f"d=s.read(4096);s.close();"
            f"print(d.decode(errors='replace'))"
            f'"'
        )
        return _ssh_run(self._host, script, timeout=timeout_secs + 10)

    def show_stats(self):
        output = self.send_command("show_stats", timeout_secs=2)
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line).get("data", {})
                except Exception:
                    pass
        return {}


class SSHFipsAdapter:
    """FIPS daemon control via SSH — same interface as FipsServiceDriver."""

    def __init__(self, host="ai-legion-small", service_name="fips", ble_adapter="hci0"):
        self._host = host
        self.service_name = service_name
        self.ble_adapter = ble_adapter

    def restart(self):
        _ssh_run(self._host, f"sudo hciconfig {self.ble_adapter} down", timeout=10)
        time.sleep(2)
        _ssh_run(self._host, f"sudo hciconfig {self.ble_adapter} up", timeout=10)
        time.sleep(1)
        _ssh_run(self._host, f"sudo systemctl restart {self.service_name}", timeout=15)

    def status(self):
        output = _ssh_run(self._host, f"sudo systemctl status {self.service_name}", timeout=10)
        if "active (running)" in output:
            return "running"
        if "inactive" in output:
            return "stopped"
        return "unknown"
