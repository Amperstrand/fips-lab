PYTHON ?= python3
INVENTORY ?= inventory/lab.yaml
RESULTS ?= results

.PHONY: setup list dry-run-smoke dry-run-lab test-microfips-smoke test-lab-3node clean-results

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

clean-results:
	find $(RESULTS) -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +
