# solver — common startup / dev commands
#
# Quick start:
#   make install     # create .venv and install the project (+ dev tools)
#   make run         # start the HTTP server (http://localhost:50051, UI at /ui)
#   make test        # run the test suite
#
# Override defaults on the command line or from the environment, e.g.:
#   make run PORT=8080
#   make install PYTHON=python3.12      # or: export PYTHON=python3.12
#   make cli INPUT=examples/sample_request.json OUTPUT=output/result.json

PYTHON ?= python3.13
VENV   := .venv
BIN    := $(VENV)/bin
PORT   ?= 50051

# uv builds virtualenvs without pip inside them — it installs packages itself —
# so the installer has to match whatever created .venv. Detected rather than
# assumed: this repo ships a uv.lock, but plain venv+pip has to keep working.
UV := $(shell command -v uv 2>/dev/null)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@echo "solver — available targets:"
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
		| sed -E 's/^([a-zA-Z_-]+):.*## (.*)/\1|\2/' \
		| awk -F'|' '{ printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2 }'
	@echo ""
	@echo "Vars: PYTHON=$(PYTHON)  PORT=$(PORT)"

$(BIN)/python: ## (internal) create the virtualenv
	@if [ -n "$(UV)" ]; then \
	  echo "uv venv --python $(PYTHON) $(VENV)"; \
	  uv venv --python $(PYTHON) $(VENV); \
	else \
	  echo "$(PYTHON) -m venv $(VENV)"; \
	  $(PYTHON) -m venv $(VENV); \
	  $(BIN)/python -m pip install --upgrade pip; \
	fi

.PHONY: venv
venv: $(BIN)/python ## Create the virtualenv (.venv)

.PHONY: install
install: $(BIN)/python ## Create venv and install the project with dev extras
	@if [ -n "$(UV)" ]; then \
	  if [ -n "$$PIP_INDEX_URL" ] && [ -z "$$UV_INDEX_URL" ] && [ -z "$$UV_DEFAULT_INDEX" ]; then \
	    echo "warning: PIP_INDEX_URL is set but uv does not read it — set UV_INDEX_URL"; \
	    echo "         (or UV_DEFAULT_INDEX) as well, or uv resolves against public PyPI."; \
	  fi; \
	  echo "uv pip install -e \".[dev]\""; \
	  uv pip install --python $(BIN)/python -e ".[dev]"; \
	elif $(BIN)/python -m pip --version >/dev/null 2>&1; then \
	  echo "pip install -e \".[dev]\""; \
	  $(BIN)/python -m pip install -e ".[dev]"; \
	else \
	  echo "error: $(VENV) has no pip and uv is not on PATH."; \
	  echo "  A uv-created venv has no pip by design. Either put uv on PATH,"; \
	  echo "  or rebuild the venv with pip: rm -rf $(VENV) && make install"; \
	  exit 1; \
	fi

.PHONY: run
run: ## Start the HTTP server (PORT=$(PORT)); web UI at /ui, API docs at /docs
	$(BIN)/python -m app.server --port $(PORT)

.PHONY: dev
dev: ## Start the HTTP server with autoreload + web UI enabled (development)
	ENABLE_UI=enable $(BIN)/uvicorn app.server:api --host 0.0.0.0 --port $(PORT) --reload

.PHONY: cli
cli: ## Run the solver in CLI mode (requires INPUT=path; optional OUTPUT=path)
	@test -n "$(INPUT)" || { echo "ERROR: set INPUT=<request.json>"; exit 1; }
	$(BIN)/python -m app.server --cli --input $(INPUT) $(if $(OUTPUT),--output $(OUTPUT),)

.PHONY: test
test: ## Run the test suite
	$(BIN)/python -m pytest

.PHONY: clean
clean: ## Remove the venv and Python caches
	rm -rf $(VENV) .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
