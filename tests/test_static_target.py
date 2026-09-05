"""Guard: the firmware static-fallback target always matches the lab
daemon's default bind (fips-lab constants are the single source of truth).
Drift here would silently point scenario nodes at the wrong endpoint
after an mDNS discovery miss (2026-09-05 soak-long nightly failure)."""

from fips_lab import bench


def test_static_target_matches_daemon_defaults():
    env = bench.lab_static_target_env()
    assert env == {
        "FIPS_TARGET_HOST": bench.LAB_DAEMON_BIND_IP,
        "FIPS_TARGET_PORT": str(bench.LAB_DAEMON_PORT),
    }


def test_lab_daemon_defaults_match_constants():
    import inspect

    sig = inspect.signature(bench.LabDaemon.__init__)
    assert sig.parameters["port"].default is bench.LAB_DAEMON_PORT
    assert sig.parameters["bind_ip"].default is bench.LAB_DAEMON_BIND_IP
