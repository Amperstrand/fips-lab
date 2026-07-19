"""fips-lab: BLE test framework for FIPS and microfips.

Now depends on tollgate-lab for shared infrastructure (SSH, ESP32 flash,
hardware lock, reporting). This module provides FIPS-specific test strategy
and drivers on top of tollgate-lab's generic device management.
"""

# Re-export tollgate-lab utilities for backward compatibility
try:
    from tollgate_lab.drivers.ssh import _ssh_run, SSHAdapter
except ImportError:
    pass

try:
    from tollgate_lab.hardware import HardwareLock
except ImportError:
    pass
