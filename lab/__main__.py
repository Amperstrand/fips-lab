from __future__ import annotations

import argparse
from pathlib import Path

from .inventory import Inventory
from .runner import LabRunner
from .scenario import Scenario


def main() -> None:
    parser = argparse.ArgumentParser(prog="fips-lab", description="Run physical FIPS/microfips lab scenarios")
    parser.add_argument("scenario", nargs="?", help="Scenario YAML path")
    parser.add_argument("--inventory", default="inventory/lab.example.yaml", help="Inventory YAML path")
    parser.add_argument("--results-dir", default="results", help="Result output directory")
    parser.add_argument("--duration", type=int, default=None, help="Override scenario duration")
    parser.add_argument("--dry-run", action="store_true", help="Create artifacts without touching devices")
    parser.add_argument("--list", action="store_true", help="List bundled scenarios")
    args = parser.parse_args()

    if args.list:
        for path in sorted(Path("scenarios").glob("*.yaml")):
            scenario = Scenario.load(path)
            print(f"{scenario.name}\t{path}")
        return

    if not args.scenario:
        parser.error("scenario is required unless --list is used")

    scenario = Scenario.load(args.scenario)
    inventory = Inventory.load(args.inventory)
    runner = LabRunner(
        scenario=scenario,
        inventory=inventory,
        results_dir=Path(args.results_dir),
        dry_run=args.dry_run,
        duration_override=args.duration,
    )
    raise SystemExit(runner.run())


if __name__ == "__main__":
    main()
