PYTHON ?= python3
INVENTORY ?= inventory/lab.yaml
RESULTS ?= results

.PHONY: setup list dry-run-smoke dry-run-lab dry-run-2node test-microfips-smoke test-lab-3node test-lab-2node publish clean-results

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
	$(PYTHON) -m lab scenarios/lab-2node-ble.yaml --inventory $(INVENTORY) --results-dir $(RESULTS)

test-lab-2node-publish:
	$(PYTHON) -m lab scenarios/lab-2node-ble.yaml --inventory $(INVENTORY) --results-dir $(RESULTS) --publish

publish:
	@RUN_DIR=$$(ls -dt $(RESULTS)/*-lab-* 2>/dev/null | head -1); \
	if [ -z "$$RUN_DIR" ]; then echo "No test results found in $(RESULTS)/"; exit 1; fi; \
	bash scripts/publish-report.sh "$$RUN_DIR"

clean-results:
	find $(RESULTS) -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +
