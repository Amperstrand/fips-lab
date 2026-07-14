"""SSH-based device adapters — standalone fallback for when labgrid is unavailable.

Same interface as the labgrid drivers. Allows tests to run with or without
labgrid. Other projects (PRTA, etc.) can import these for SSH-based device
control without a labgrid dependency.
"""

import json
import subprocess
import time


def _ssh_run(host, cmd, timeout=30, stdin_data=None):
    kwargs = dict(
        args=["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"ubuntu@{host}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if stdin_data is not None:
        kwargs["input"] = stdin_data
    result = subprocess.run(**kwargs)
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

    def flash(self, firmware_path_on_build_host, build_host="ai-legion-small"):
        """Convert ELF to binary, copy to this host, flash to ESP32."""
        binary_path = "/tmp/fips-flash.bin"

        _ssh_run(
            build_host,
            f"export PATH=/home/ubuntu/.rustup/toolchains/esp/bin:$PATH && "
            f"esptool --chip esp32 elf2image {firmware_path_on_build_host} "
            f"--output {binary_path}",
            timeout=60,
        )

        binary_data = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", f"ubuntu@{build_host}", f"cat {binary_path}"],
            capture_output=True, timeout=30,
        ).stdout

        _ssh_run(self._host, f"cat > {binary_path}", timeout=30, stdin_data=binary_data)

        return _ssh_run(
            self._host,
            f"sudo esptool --chip esp32 --port {self.serial_port} "
            f"--before default-reset -b 460800 "
            f"write-flash 0x10000 {binary_path}",
            timeout=120,
        )


class SSHFipsAdapter:
    """FIPS daemon control via SSH — same interface as FipsServiceDriver."""

    def __init__(self, host="ai-legion-small", service_name="fips", ble_adapter="hci0"):
        self._host = host
        self.service_name = service_name
        self.ble_adapter = ble_adapter
        self._fipsctl = f"/home/ubuntu/src/fips/target/release/fipsctl"

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

    def has_peer(self, npub):
        output = _ssh_run(self._host, f"sudo {self._fipsctl} has-peer {npub}", timeout=10)
        return "true" in output.lower() or "yes" in output.lower()

    def show_peers(self):
        output = _ssh_run(self._host, f"sudo {self._fipsctl} show-peers", timeout=10)
        try:
            return json.loads(output)
        except Exception:
            return []


class SSHFirmwareBuilder:
    """Build firmware on a remote host — same interface as a labgrid build driver."""

    def __init__(self, host="ai-legion-small", repo_path="/home/ubuntu/src/microfips"):
        self._host = host
        self._repo = repo_path

    def build(self, features="l2cap", timeout=600):
        """Pull latest + build ESP32 firmware. Returns remote ELF path."""
        env_setup = (
            "export PATH=/home/ubuntu/.rustup/toolchains/esp/bin:"
            "/home/ubuntu/.rustup/toolchains/esp/xtensa-esp-elf/"
            "esp-15.2.0_20250920/xtensa-esp-elf/bin:"
            "/home/ubuntu/.cargo/bin:$PATH && "
            "export LIBCLANG_PATH=/home/ubuntu/.rustup/toolchains/esp/"
            "xtensa-esp32-elf-clang/esp-20.1.1_20250829/esp-clang/lib && "
            "export RUSTUP_TOOLCHAIN=esp"
        )

        output = _ssh_run(
            self._host,
            f"cd {self._repo} && git fetch origin && git reset --hard origin/main && "
            f"{env_setup} && "
            f"cargo build -p microfips-esp32 --release "
            f"--target xtensa-esp32-none-elf "
            f"-Zbuild-std=core,alloc --features {features} 2>&1 | tail -1 && "
            f"echo BUILD_OK",
            timeout=timeout,
        )

        if "BUILD_OK" not in output:
            raise RuntimeError(f"Firmware build failed: {output[-300:]}")

        return (
            f"{self._repo}/target/xtensa-esp32-none-elf/release/microfips-esp32-l2cap"
        )
