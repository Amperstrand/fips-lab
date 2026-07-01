#!/usr/bin/env python3
"""Publish fips-lab benchmark and scenario results to Blossom + Nostr.

Thin wrapper around lib.result_publisher that adapts fips-lab's data
formats (single benchmark JSON or scenario results directory) to the
directory-based input result_publisher expects.

Sets PROJECT_TAG so runs appear on tests.tollgate.me under the correct
project filter (fips-ble for BLE scenarios, fips-benchmark for benchmarks).

Usage:
    # Publish a single benchmark JSON
    python3 scripts/publish-results.py results/benchmark-matrix/2026-05-18T10-24-59Z.json

    # Publish a scenario results directory
    python3 scripts/publish-results.py results/20260518T102459Z-lab-2node-ble/

    # Dry run (no uploads)
    python3 scripts/publish-results.py --dry-run results/benchmark-matrix/sample.json

    # Custom project tag
    python3 scripts/publish-results.py --project-tag fips-ble results/scenario-dir/
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        help="Path to benchmark JSON file OR scenario results directory",
    )
    parser.add_argument(
        "--nsec-file",
        default=os.environ.get("NSEC_FILE", ".nsec"),
        help="NSEC file for signing Nostr events (default: .nsec)",
    )
    parser.add_argument(
        "--project-tag",
        default=os.environ.get("PROJECT_TAG", "fips-ble"),
        help="Nostr #t tag for project filtering (default: fips-ble)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: {input_path} does not exist", file=sys.stderr)
        return 1

    if not Path(args.nsec_file).exists() and not args.dry_run:
        print(f"ERROR: nsec file not found at {args.nsec_file}", file=sys.stderr)
        print("Create it with: echo <hex-nsec> > .nsec", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parent.parent
    results_dir: Path

    if input_path.is_file() and input_path.suffix == ".json":
        results_dir = _stage_benchmark_json(input_path, repo_root)
    elif input_path.is_dir():
        results_dir = input_path
    else:
        print(f"ERROR: {input_path} is neither a JSON file nor a directory", file=sys.stderr)
        return 1

    env = os.environ.copy()
    env["PROJECT_TAG"] = args.project_tag

    cmd = [
        sys.executable, "-m", "lib.result_publisher",
        str(results_dir),
        "--nsec-file", args.nsec_file,
    ]
    if args.dry_run:
        cmd.append("--dry-run")

    print(f"Publishing {results_dir} with PROJECT_TAG={args.project_tag}...")
    if args.dry_run:
        print(f"[DRY RUN] Would run: {' '.join(cmd)}")

    result = subprocess.run(cmd, env=env, cwd=str(repo_root))
    return result.returncode


def _stage_benchmark_json(json_path: Path, repo_root: Path) -> Path:
    """Copy a single benchmark JSON into a temp directory that result_publisher can scan."""
    tmpdir = Path(tempfile.mkdtemp(prefix="fips-lab-publish-"))
    shutil.copy2(json_path, tmpdir / "benchmark-results.json")

    git_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=repo_root,
    ).stdout.strip()[:12]

    git_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=repo_root,
    ).stdout.strip()

    done = {
        "status": "completed",
        "run_id": json_path.stem,
        "scenario": "benchmark-matrix",
        "fips_lab_commit": git_commit,
        "fips_lab_branch": git_branch,
        "completed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (tmpdir / "DONE").write_text(json.dumps(done, indent=2))

    print(f"  Staged benchmark JSON in {tmpdir}")
    return tmpdir


if __name__ == "__main__":
    sys.exit(main())
