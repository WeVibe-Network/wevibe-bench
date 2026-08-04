.PHONY: test test-fast test-file test-name test-slowest test-all

test:
	@TIMESTAMP=$$(date +%Y%m%dT%H%M%S) && \
	mkdir -p runs && \
	.venv/bin/python -m pytest -v > runs/pytest-$$TIMESTAMP.log 2>&1; \
	CODE=$$?; \
	cp runs/pytest-$$TIMESTAMP.log runs/pytest-last.log; \
	cd runs && ls -1t pytest-*.log | tail -n +11 | xargs -r rm --; \
	exit $$CODE

test-fast:
	@$(MAKE) test || true
	@echo "Running fast tests only (skipping slow)..."
	@TIMESTAMP=$$(date +%Y%m%dT%H%M%S) && \
	mkdir -p runs && \
	pytest -v -m "not slow" > runs/pytest-$$TIMESTAMP.log 2>&1; \
	CODE=$$?; \
	cp runs/pytest-$$TIMESTAMP.log runs/pytest-last.log; \
	cd runs && ls -1t pytest-*.log | tail -n +11 | xargs -r rm --; \
	exit $$CODE

test-file:
	@echo "Usage: make test-file FILE=tests/test_foo.py"
	@TIMESTAMP=$$(date +%Y%m%dT%H%M%S) && \
	mkdir -p runs && \
	pytest -v $(FILE) > runs/pytest-$$TIMESTAMP.log 2>&1; \
	CODE=$$?; \
	cp runs/pytest-$$TIMESTAMP.log runs/pytest-last.log; \
	cd runs && ls -1t pytest-*.log | tail -n +11 | xargs -r rm --; \
	exit $$CODE

test-name:
	@echo "Usage: make test-name NAME=substring"
	@TIMESTAMP=$$(date +%Y%m%dT%H%M%S) && \
	mkdir -p runs && \
	pytest -v -k $(NAME) > runs/pytest-$$TIMESTAMP.log 2>&1; \
	CODE=$$?; \
	cp runs/pytest-$$TIMESTAMP.log runs/pytest-last.log; \
	cd runs && ls -1t pytest-*.log | tail -n +11 | xargs -r rm --; \
	exit $$CODE

test-slowest:
	@TIMESTAMP=$$(date +%Y%m%dT%H%M%S) && \
	mkdir -p runs && \
	pytest -v --durations=10 > runs/pytest-$$TIMESTAMP.log 2>&1; \
	CODE=$$?; \
	cp runs/pytest-$$TIMESTAMP.log runs/pytest-last.log; \
	cd runs && ls -1t pytest-*.log | tail -n +11 | xargs -r rm --; \
	exit $$CODE

test-all:
	@TIMESTAMP=$$(date +%Y%m%dT%H%M%S) && \
	mkdir -p runs && \
	pytest -v -m "" > runs/pytest-$$TIMESTAMP.log 2>&1; \
	CODE=$$?; \
	cp runs/pytest-$$TIMESTAMP.log runs/pytest-last.log; \
	cd runs && ls -1t pytest-*.log | tail -n +11 | xargs -r rm --; \
	exit $$CODE
