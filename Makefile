.DEFAULT_GOAL := help
SHELL := /bin/bash

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: help install up down logs provider loadtest test lint fmt stats bundle clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create a virtualenv and install dependencies
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"
	@echo "installed -- activate with: source $(VENV)/bin/activate"

up: ## Start redis and the fake provider
	docker compose up -d --build
	@echo "waiting for the provider..."
	@for i in $$(seq 1 30); do \
	  curl -sf http://localhost:8080/health >/dev/null && { echo "provider ready"; exit 0; }; \
	  sleep 1; \
	done; echo "provider did not become ready"; docker compose logs provider; exit 1

down: ## Stop everything
	docker compose down -v

logs: ## Tail the provider logs
	docker compose logs -f provider

provider: ## Run the fake provider locally instead of in docker
	$(PY) -m uvicorn fake_provider.main:app --host 0.0.0.0 --port 8080

loadtest: ## Run the load test (the gate)
	$(PY) -m loadtest.run

test: ## Run the test suite
	$(PY) -m pytest -q

lint: ## Lint
	$(PY) -m ruff check .

fmt: ## Format
	$(PY) -m ruff format .

stats: ## Print the provider's own counters
	@curl -s http://localhost:8080/admin/stats | $(PY) -m json.tool

bundle: ## Build the candidate archive (excludes interviewer/)
	@mkdir -p dist
	@rm -f dist/rate-test-candidate.zip
	@git archive --format=zip --output=dist/rate-test-candidate.zip HEAD \
	  $$(git ls-tree --name-only HEAD | grep -v '^interviewer$$')
	@echo "wrote dist/rate-test-candidate.zip (interviewer/ excluded)"
	@echo "contents:" && unzip -l dist/rate-test-candidate.zip | tail -n +4 | head -30

clean: ## Remove build and cache artefacts
	rm -rf dist .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
