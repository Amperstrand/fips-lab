# Tracked test skip-list — fips-lab

Closes #8 (CI half). The hardware suite runs on real targets only via
`pytest --lg-env=<environment.yaml>`; in CI (and on any host without a labgrid
environment) the suite skips **by design**. This file is the contract that makes
those skips honest: every skip reason that may appear in CI is listed here, and
the CI `skip-reason guard` step fails the build if a reason outside this list
shows up (a new reason usually means a test broke rather than skipped cleanly).

## Skip reason classes (tracked)

| Reason (as printed by `pytest -rs`) | Class | What unskips it |
|---|---|---|
| `missing environment config (use --lg-env)` | environment | run with `--lg-env` pointing at a labgrid environment (`environment.yaml` = macbook-local + ai-legion-small + esp32 targets) |
| `ESP32 cannot initiate benchmarks` | capability | benchmark initiator support on ESP32 firmware |
| `Download direction unimplemented, issue #132` | upstream feature | download-direction benchmarking (fips issue #132) |

## Per-file state (host run, 2026-09-06)

| File | Tests | Skipped via |
|---|---|---|
| `tests/test_echo.py` | 1 | environment |
| `tests/test_strategy.py` | 4 | environment |
| `tests/test_benchmark.py` | ~26 | environment; capability (ESP32-initiated ×2 classes); issue #132 ×2 |
| `tests/test_scenarios.py` | ~35 | environment |
| `tests/test_issues.py` | ~8 | environment |
| `tests/test_esp32_l2cap.py` | all | `hardware` marker (excluded from CI selection) |
| `tests/test_flash_and_verify.py` | all | `flash`/`hardware` (destructive; excluded from CI selection) |

## What CI actually gates

1. `uv sync` — dependency lock resolves.
2. `pytest --collect-only` — every test module imports; fixtures and markers
   resolve. This alone catches most breakage (a missing dep or a bad import
   fails here, not at 2am on the rig).
3. `pytest -m "not hardware and not flash"` — must pass with only the skip
   reasons above; any ERROR/Failure or untracked reason fails the build.

## Running the full suite

```bash
uv sync
pytest --lg-env=environment.yaml -v          # macbook-local target
pytest --lg-env=environment-coordinator.yaml # coordinator-managed rig
```
