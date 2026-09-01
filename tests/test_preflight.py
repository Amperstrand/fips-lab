"""Bench preflight: assert the rig is in the state scenarios need.

The cheap hardware gate (bolty-rs pattern): if this fails, every
mutation scenario (flash/rekey/link-death) would waste its cycle —
fail here first, in seconds, with a precise message. Non-mutation
checks are warn-only.
"""

import subprocess

import pytest

from fips_lab import bench

S3_LAB_SERIAL = "F4:12:FA:CF:03:84"


@pytest.mark.hardware
def test_preflight_rig():
    failures = []

    port = bench.find_board(serial=S3_LAB_SERIAL)
    if port is None:
        failures.append(f"primary bench board {S3_LAB_SERIAL} not attached")
    else:
        # The safety contract must permit what the scenarios do.
        try:
            bench.require_board(S3_LAB_SERIAL, "flash")
        except bench.BoardError as e:
            failures.append(f"registry refuses the primary board: {e}")

        # Off-limits hardware must not be flashable even if attached:
        # the M5 Stack (FTDI 0403:6001) is deliberately unlisted.
        for p_std in ("/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"):
            import pathlib
            p = pathlib.Path(p_std)
            if not p.exists():
                continue
            try:
                props = subprocess.run(
                    ["udevadm", "info", "-q", "property", str(p)],
                    capture_output=True, text=True, timeout=10,
                ).stdout
                if "0403:6001" in props.replace("PRODUCT=", "").replace("/", ":"):
                    try:
                        bench.require_board("Hades2001", "flash")
                        failures.append(
                            "M5 Stack present AND registry would allow it — contract broken"
                        )
                    except bench.BoardError:
                        pass  # correct: refused
            except subprocess.TimeoutExpired:
                failures.append(f"udevadm timed out on {p}")

    # Warn-only: daemon binary presence (mutation scenarios need it).
    if not bench.FIPS_BIN.exists():
        print(f"WARN: fips binary missing at {bench.FIPS_BIN}")

    assert not failures, "preflight failures:\\n" + "\\n".join(failures)
