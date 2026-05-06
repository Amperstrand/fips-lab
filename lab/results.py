from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def create_run_dir(base: Path, scenario_name: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = base / f"{timestamp}-{scenario_name}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def copy_scenario(src: Path, dst_dir: Path) -> None:
    shutil.copy2(src, dst_dir / "scenario.yaml")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()
