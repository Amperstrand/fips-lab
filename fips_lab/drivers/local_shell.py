"""Local shell driver for executing commands on the host machine."""

import subprocess

import attr

from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.protocol.commandprotocol import CommandProtocol


@target_factory.reg_driver
@attr.s(eq=False)
class LocalShellDriver(Driver, CommandProtocol):
    """Execute commands locally via subprocess.

    Implements CommandProtocol for targets that run on the local machine
    (e.g. the MacBook controller) without requiring SSH.
    """

    @Driver.check_active
    def run(self, cmd: str):
        """Run *cmd* and return (stdout, stderr, returncode)."""
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return (result.stdout, result.stderr, result.returncode)

    @Driver.check_active
    def run_check(self, cmd: str):
        """Run *cmd*; return stdout.  Raise on non-zero exit."""
        stdout, stderr, rc = self.run(cmd)
        if rc != 0:
            raise Exception(
                f"Command failed ({rc}): {cmd}\n{stderr}"
            )
        return stdout

    @Driver.check_active
    def get_status(self):
        raise NotImplementedError

    @Driver.check_active
    def wait_for(self):
        raise NotImplementedError

    @Driver.check_active
    def poll_until_success(self):
        raise NotImplementedError
