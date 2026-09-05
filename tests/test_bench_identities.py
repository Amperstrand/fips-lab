"""Per-run bench identities (#206): fresh high-entropy keys each scenario
run, so published artifacts (verdicts, console logs, issue comments) only
ever contain single-use npubs. These tests pin the derivation contract:
deterministic per (seed, label), distinct across both axes, never the
public G*N scalars, nsecs never persisted, and both daemon classes embed
a provided identity in their generated configs."""

import json
from pathlib import Path

from fips_lab import bench

REPO = bench.MICROFIPS_REPO
SEED = "ab" * 16


def test_seeded_identities_deterministic_and_distinct():
    a = bench.BenchIdentities(REPO, seed=SEED)
    b = bench.BenchIdentities(REPO, seed=SEED)
    assert a.npub("daemon") == b.npub("daemon")
    assert a.nsec("daemon") == b.nsec("daemon")
    assert a.node_addr("daemon") == b.node_addr("daemon")
    assert a.npub("daemon") != a.npub("node")
    assert a.nsec("daemon") != a.nsec("node")


def test_seeded_identities_are_not_generator_multiples():
    """The whole point: no scalar may be a small integer (G*N convention) —
    a published npub must not be reproducible from public knowledge."""
    a = bench.BenchIdentities(REPO, seed=SEED)
    for label in ("daemon", "node"):
        scalar = int(a.nsec(label), 16)
        assert scalar > 2**240, f"{label} scalar looks low-entropy"


def test_different_seeds_different_keys():
    a = bench.BenchIdentities(REPO, seed="ab" * 16)
    b = bench.BenchIdentities(REPO, seed="cd" * 16)
    assert a.npub("daemon") != b.npub("daemon")


def test_save_records_publics_but_never_nsecs(tmp_path):
    a = bench.BenchIdentities(REPO, seed=SEED)
    daemon_npub = a.npub("daemon")
    a.nsec("node")  # both cached
    a.save(tmp_path)
    doc = json.loads((tmp_path / "identities.json").read_text())
    assert doc["seed"] == SEED
    assert doc["identities"]["daemon"]["npub_hex"] == daemon_npub
    raw = (tmp_path / "identities.json").read_text()
    assert a.nsec("daemon") not in raw and a.nsec("node") not in raw, \
        "private keys must never be persisted"
    # Reproducibility: a fresh bundle from the recorded seed matches.
    again = bench.BenchIdentities(REPO, seed=doc["seed"])
    assert again.npub("daemon") == daemon_npub


def test_lab_daemon_config_embeds_provided_identity(tmp_path):
    nsec = bench.BenchIdentities(REPO, seed=SEED).nsec("daemon")
    d = bench.LabDaemon(REPO, 32, tmp_path, nsec_hex=nsec)
    template = (REPO / "tools/fips-lab.yaml").read_text()
    cfg = d._render_config(template, nsec)
    assert f"nsec: {nsec}" in cfg
    assert "__LAB_DAEMON_NSEC__" not in cfg
    # The G*N default must NOT appear when an override is given.
    assert "0000000000000000000000000000000000000000000000000000000000000008" not in cfg


def test_lab_daemon_default_identity_unchanged(tmp_path):
    """Compat: scenarios not yet migrated still get the G*8 identity."""
    d = bench.LabDaemon(REPO, 32, tmp_path)
    assert d.nsec_hex is None
    template = (REPO / "tools/fips-lab.yaml").read_text()
    cfg = d._render_config(
        template, bench.lab_daemon_nsec(REPO, d.generator_mul)
    )
    assert bench.lab_nsec(REPO, 8) in cfg
