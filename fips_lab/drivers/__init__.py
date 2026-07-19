"""FIPS-specific labgrid drivers.

Shared infrastructure (ESP32 flash, SSH, hardware lock) now comes from
tollgate-lab. This module provides FIPS-specific drivers that extend
tollgate-lab's base capabilities.
"""

# Re-export from tollgate_lab for backward compat
try:
    from tollgate_lab.drivers.esp_flash import EspFlashDriver
except ImportError:
    from fips_lab.drivers.esp_flash import EspFlashDriver

try:
    from tollgate_lab.drivers.fips_service import FipsServiceDriver
except ImportError:
    from fips_lab.drivers.fips_service import FipsServiceDriver

try:
    from tollgate_lab.drivers.fipsctl import FipsctlDriver
except ImportError:
    from fips_lab.drivers.fipsctl import FipsctlDriver

# FIPS-specific (not in tollgate_lab)
from fips_lab.drivers.local_shell import LocalShellDriver
from fips_lab.drivers.esp_serial import EspSerialDriver
from fips_lab.drivers.ble_scan import BLEScanDriver

__all__ = [
    "EspFlashDriver",
    "FipsServiceDriver",
    "FipsctlDriver",
    "LocalShellDriver",
    "EspSerialDriver",
    "BLEScanDriver",
]
