"""SSH adapters for FIPS lab tests.

Delegates to tollgate_lab.drivers.ssh when available.
Falls back to local implementation for standalone operation.
"""

try:
    from tollgate_lab.drivers.ssh import _ssh_run, SSHAdapter
except ImportError:
    # Fallback — keep local copies for standalone operation
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
