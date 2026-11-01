# BabyScout Pico W - host-side tasks.
#
# On WSL the device is a Windows COM port, so mpremote must run through
# python.exe. On Linux/macOS set MPREMOTE/PORT to the native equivalents.
#
#   make test
#   make deploy PORT=COM6
#   make verify PORT=COM6
#   make logs   PORT=COM6

PORT ?= COM6
PYTHON ?= python3
MPREMOTE ?= python.exe -m mpremote

SOURCES = main.py lib/*.py picoconnection/__init__.py

.PHONY: test compile check deploy verify reset logs clean

test:
	$(PYTHON) -m pytest -q

compile:
	$(PYTHON) -m py_compile $(SOURCES)
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	@echo "compile OK"

check: compile test

deploy:
	./scripts/deploy.sh $(PORT)

verify:
	./scripts/verify.sh $(PORT)

reset:
	$(MPREMOTE) connect $(PORT) reset

# Interactive serial console (Ctrl+] or Ctrl+X to leave, depending on mpremote).
logs:
	$(MPREMOTE) connect $(PORT) repl

clean:
	rm -rf __pycache__ lib/__pycache__ picoconnection/__pycache__ \
	       tests/__pycache__ .pytest_cache .ruff_cache .deploy_check
	@echo "clean OK"
