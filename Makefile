SHELL=/bin/bash
DEST_DIR := $(shell python3 -m site --user-base)/bin

test:
	python3 -m unittest discover . -p "*_test.py"

requirements: requirements.txt
	python3 -m pip install -r requirements.txt

bumpversion:
	pipenv run bumpversion --commit minor

build: setup.py
	python3 ./setup.py bdist_wheel sdist

install: setup.py
	python3 -m pip install . 
	@echo "Executable: $(DEST_DIR)/gce-rescue"
