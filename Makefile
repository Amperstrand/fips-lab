PYTHON ?= python3
INVENTORY ?= inventory/lab.yaml
RESULTS ?= results
COMMIT ?= HEAD

.PHONY: setup list dry-run-smoke dry-run-lab dry-run-2node dry-run-campaign-ble test-microfips-smoke test-lab-3node test-lab-2node test-lab-2node-linux-init test-campaign-ble test-campaign-ble-20min test-commit test-lab-2node-commit publish publish-benchmarks setup-218-phase2 clean-results test-labgrid test-labgrid-echo test-labgrid-benchmark test-labgrid-strategy test-labgrid-issues test-labgrid-campaign

setup:
	$(PYTHON) -m pip install -r requirements.txt

list:
	$(PYTHON) -m lab --list

dry-run-smoke:
	$(PYTHON) -m lab scenarios/microfips-smoke.yaml --inventory inventory/lab.example.yaml --results-dir $(RESULTS) --dry-run

dry-run-lab:
	$(PYTHON) -m lab scenarios/lab-3node-isolated.yaml --inventory inventory/lab.example.yaml --results-dir $(RESULTS) --dry-run

test-microfips-smoke:
	$(PYTHON) -m lab scenarios/microfips-smoke.yaml --inventory $(INVENTORY) --results-dir $(RESULTS)

test-lab-3node:
	$(PYTHON) -m lab scenarios/lab-3node-isolated.yaml --inventory $(INVENTORY) --results-dir $(RESULTS)

dry-run-2node:
	$(PYTHON) -m lab scenarios/lab-2node-ble.yaml --inventory inventory/lab.example.yaml --results-dir $(RESULTS) --dry-run

test-lab-2node:
	@echo "DEPRECATED: use 'make test-labgrid' instead."
	$(PYTHON) -m lab scenarios/lab-2node-ble.yaml --inventory $(INVENTORY) --results-dir $(RESULTS)

test-lab-2node-publish:
	$(PYTHON) -m lab scenarios/lab-2node-ble.yaml --inventory $(INVENTORY) --results-dir $(RESULTS) --publish

test-commit:
	$(PYTHON) -m lab $(SCENARIO) --inventory $(INVENTORY) --results-dir $(RESULTS) --commit $(COMMIT)

test-lab-2node-commit:
	$(PYTHON) -m lab scenarios/lab-2node-ble.yaml --inventory $(INVENTORY) --results-dir $(RESULTS) --commit $(COMMIT)

test-lab-2node-linux-init:
	$(PYTHON) -m lab scenarios/lab-2node-ble-linux-init.yaml --inventory $(INVENTORY) --results-dir $(RESULTS)

dry-run-campaign-ble:
	$(PYTHON) -m lab --campaign scenarios/campaign-ble-bidirectional.yaml --inventory inventory/lab.example.yaml --results-dir $(RESULTS) --dry-run

test-campaign-ble:
	$(PYTHON) -m lab --campaign scenarios/campaign-ble-bidirectional.yaml --inventory $(INVENTORY) --results-dir $(RESULTS)

test-campaign-ble-20min:
	$(PYTHON) -m lab --campaign scenarios/campaign-ble-bidirectional-20min.yaml --inventory $(INVENTORY) --results-dir $(RESULTS) --publish

publish:
	@RUN_DIR=$$(ls -dt $(RESULTS)/*-lab-* 2>/dev/null | head -1); \
	if [ -z "$$RUN_DIR" ]; then echo "No test results found in $(RESULTS)/"; exit 1; fi; \
	python3 scripts/publish-results.py "$$RUN_DIR" --project-tag fips-ble

publish-benchmarks:
	@LATEST_JSON=$$(ls -t $(RESULTS)/benchmark-matrix/*.json 2>/dev/null | head -1); \
	if [ -z "$$LATEST_JSON" ]; then echo "No benchmarks found in $(RESULTS)/benchmark-matrix/"; exit 1; fi; \
	python3 scripts/publish-results.py "$$LATEST_JSON" --project-tag fips-benchmark

setup-218-phase2:
	bash scripts/setup-218-phase2.sh

setup-218-phase2-dry-run:
	bash scripts/setup-218-phase2.sh --dry-run

clean-results:
	find $(RESULTS) -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +

WAIT_PEERS_TIMEOUT ?= 180

test-labgrid:
	$(PYTHON) -m pytest --lg-env=environment.yaml tests/ -v --wait-peers-timeout=$(WAIT_PEERS_TIMEOUT)

test-labgrid-echo:
	$(PYTHON) -m pytest --lg-env=environment.yaml tests/test_echo.py -v

test-labgrid-benchmark:
	$(PYTHON) -m pytest --lg-env=environment.yaml tests/test_benchmark.py -v --wait-peers-timeout=$(WAIT_PEERS_TIMEOUT)

test-labgrid-strategy:
	$(PYTHON) -m pytest --lg-env=environment.yaml tests/test_strategy.py -v

test-labgrid-issues:
	$(PYTHON) -m pytest --lg-env=environment.yaml tests/test_issues.py -v

test-labgrid-campaign:
	$(PYTHON) -m pytest --lg-env=environment.yaml tests/ -v --wait-peers-timeout=300 -k "benchmark or echo or strategy or issues or scenarios"
