"""Driver for the fipsctl CLI tool."""

import json

import attr

from labgrid import target_factory
from labgrid.driver import Driver
from labgrid.protocol.commandprotocol import CommandProtocol


@target_factory.reg_driver
@attr.s(eq=False)
class FipsctlDriver(Driver):
    """High-level wrapper around the ``fipsctl`` binary.

    Binds to a :class:`CommandProtocol` provider (e.g.
    :class:`LocalShellDriver` or an SSH-based shell driver) and exposes
    fipsctl commands as Python methods.
    """

    bindings = {"shell": "CommandProtocol"}

    binary = attr.ib(validator=attr.validators.instance_of(str))
    sudo = attr.ib(default=True, validator=attr.validators.instance_of(bool))

    def _build_cmd(self, *args: str) -> str:
        """Build the full shell command string."""
        prefix = "sudo " if self.sudo else ""
        parts = " ".join(args)
        return f"{prefix}{self.binary} {parts}"

    @Driver.check_active
    def run_fipsctl(self, *args: str) -> dict:
        """Run an arbitrary fipsctl command and return parsed JSON."""
        cmd = self._build_cmd(*args)
        output = self.shell.run_check(cmd)
        # SSHDriver returns list[str], LocalShellDriver returns str
        if isinstance(output, list):
            output = "\n".join(output)
        return json.loads(output.strip())

    @Driver.check_active
    def show_status(self) -> dict:
        """Return the current fips daemon status."""
        return self.run_fipsctl("show", "status")

    @Driver.check_active
    def show_peers(self) -> list:
        """Return the list of known peers."""
        result = self.run_fipsctl("show", "peers")
        if isinstance(result, dict):
            return result.get("peers", [])
        return result

    @Driver.check_active
    def has_peer(self, npub: str) -> bool:
        for peer in self.show_peers():
            if peer.get("npub", "").startswith(npub[:16]):
                return True
        return False

    @Driver.check_active
    def benchmark_echo(
        self,
        peer: str,
        count: int = 10,
        payload_size: int = 0,
    ) -> dict:
        """Run an echo benchmark against *peer*.

        Returns the parsed JSON result.  The result may contain
        ``{"status": "pending"}`` if the benchmark has not yet completed.
        """
        return self.run_fipsctl(
            "benchmark", "echo",
            "--peer", str(peer),
            "--count", str(count),
            "--payload-size", str(payload_size),
            "--json",
        )

    @Driver.check_active
    def benchmark_throughput(
        self,
        peer: str,
        direction: str = "upload",
        duration: int = 5,
        frame_size: int = 100,
        rate: int = 40000,
    ) -> dict:
        """Run a throughput benchmark against *peer*.

        Returns the parsed JSON result.  The result may contain
        ``{"status": "pending"}`` if the benchmark has not yet completed.
        """
        return self.run_fipsctl(
            "benchmark", "throughput",
            "--peer", str(peer),
            "--direction", direction,
            "--duration", str(duration),
            "--frame-size", str(frame_size),
            "--rate", str(rate),
            "--json",
        )
