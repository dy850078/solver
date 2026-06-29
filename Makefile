# solver — common startup / dev commands
#
# Quick start:
#   make install     # create .venv and install the project (+ dev tools)
#   make run         # start the HTTP server (http://localhost:50051, UI at /ui)
#   make test        # run the test suite
#
# Override defaults on the command line, e.g.:
#   make run PORT=8080
#   make cli INPUT=examples/sample_request.json OUTPUT=output/result.json

PYTHON ?= python3.13
VENV   := .venv
BIN    := $(VENV)/bin
PORT   ?= 50051

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
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip

.PHONY: venv
venv: $(BIN)/python ## Create the virtualenv (.venv)

.PHONY: install
install: $(BIN)/python ## Create venv and install the project with dev extras
	$(BIN)/python -m pip install -e ".[dev]"

.PHONY: run
run: ## Start the HTTP server (PORT=$(PORT)); web UI at /ui, API docs at /docs
	$(BIN)/python -m app.server --port $(PORT)

.PHONY: dev
dev: ## Start the HTTP server with autoreload (development)
	$(BIN)/uvicorn app.server:api --host 0.0.0.0 --port $(PORT) --reload

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
