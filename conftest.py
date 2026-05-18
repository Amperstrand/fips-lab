import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fips_lab  # registers custom drivers with labgrid's target_factory

MAC_NPUB = "npub1uwwvvqqqkrkp58txtkaevw20wvqr64rlkhsunwlegfe9lyz9q2asww7dem"
LINUX_NPUB = "npub1peaqmgq6y4wduyr2yqh0fatnvah0ncj0rjqhd5p6aqaz5wsr05ssu0cnha"
ESP32_NPUB = "npub1ccz8l9zpa47k6vz9gphftsrumpw80rjt3nhnefat4symjhrsnmjs38mnyd"


def _require_peer(linux_target, npub: str):
    linux_target.activate("FipsctlDriver")
    fipsctl = linux_target.get_driver("FipsctlDriver")
    if not fipsctl.has_peer(npub):
        pytest.skip(f"Peer {npub[:12]}... not connected")


@pytest.fixture
def with_mac_peer(linux_target):
    _require_peer(linux_target, MAC_NPUB)


@pytest.fixture
def with_esp32_peer(linux_target):
    _require_peer(linux_target, ESP32_NPUB)


@pytest.fixture
def linux_target(env):
    return env.get_target("linux-218")


@pytest.fixture
def mac_target(env):
    return env.get_target("macbook-local")


@pytest.fixture
def esp32_target(env):
    return env.get_target("esp32-d0wd-01")


@pytest.fixture(scope="session")
def benchmark_results(request):
    """Collect benchmark results; writes to ``results/benchmark-matrix/``."""
    results: list[dict[str, object]] = []
    yield results
    if not results:
        return
    output_dir = Path(request.config.rootdir) / "results" / "benchmark-matrix"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    output_file = output_dir / f"{timestamp}.json"
    with open(output_file, "w") as fh:
        json.dump(
            {"timestamp": timestamp, "scenario": "benchmark-matrix", "results": results},
            fh,
            indent=2,
        )
