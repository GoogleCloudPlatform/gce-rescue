SHELL=/bin/bash
DEST_DIR := $(shell python3 -m site --user-base)/bin

# V1 tests (unittest)
test:
	python3 -m unittest discover . -p "*_test.py"

# V2 tests (pytest)
test-v2:
	cd gce_rescue_v2 && python3 -m pytest tests/ -v --tb=short

# Run all tests
test-all: test test-v2

requirements: requirements.txt
	python3 -m pip install -r requirements.txt

# V2 requirements
requirements-v2:
	python3 -m pip install -r gce_rescue_v2/requirements.txt

bumpversion:
	pipenv run bumpversion --commit minor

build: setup.py
	python3 ./setup.py bdist_wheel sdist

install: setup.py
	python3 -m pip install .
	@echo "V2 Executable: $(DEST_DIR)/gce-rescue"
	@echo "V1 Legacy:     $(DEST_DIR)/gce-rescue-v1"

# Lint V2 code
lint-v2:
	cd gce_rescue_v2 && flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics

# Clean build artifacts
clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ __pycache__/
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
