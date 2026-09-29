# Developer entry points. Design and conventions: CLAUDE.md.
# Run `make` (or `make help`) to list targets.

.DEFAULT_GOAL := help
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

UV        ?= uv
FEATURES  ?= features/
OUT       ?= artifacts/
BASE_URL  ?=
DEMO_PORT ?= 8765
# Pinned Playwright MCP server. The browser is installed with the Playwright CLI bundled in
# this exact package so the Chromium build matches. Must equal the default in
# src/agentic_e2e/config.py (a unit test checks); exported so overrides reach the runner.
PLAYWRIGHT_MCP_VERSION ?= 0.0.83
export AGENTIC_E2E_PLAYWRIGHT_MCP_VERSION := $(PLAYWRIGHT_MCP_VERSION)

.PHONY: help setup install lock browser lint format format-check typecheck test test-browser \
        test-live check ci validate run summarize demo-site clean clean-artifacts

help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Setup -------------------------------------------------------------------

setup: install browser ## Fresh Codespace setup: Python deps + headless Chromium

install: ## Install Python deps, including the dev group
	$(UV) sync $(UV_SYNC_FLAGS)

lock: ## Refresh uv.lock after editing dependencies in pyproject.toml
	$(UV) lock

browser: ## Install Chromium and its OS deps for the pinned Playwright MCP server
	npx -y -p @playwright/mcp@$(PLAYWRIGHT_MCP_VERSION) playwright install --with-deps chromium

# --- Quality gates -----------------------------------------------------------

lint: ## Ruff lint, including the MCP/wait ban outside browser/
	$(UV) run ruff check .

format: ## Sort imports and format code
	$(UV) run ruff check --select I --fix .
	$(UV) run ruff format .

format-check: ## Fail if code is not formatted
	$(UV) run ruff format --check .

typecheck: ## mypy --strict over src/
	$(UV) run mypy

test: ## Unit tests: no network, browser or API key (extra args: PYTEST_ARGS=...)
	$(UV) run pytest $(PYTEST_ARGS)

test-browser: ## Browser integration tests: real Playwright MCP + Chromium, no API
	$(UV) run pytest -m browser $(PYTEST_ARGS)

test-live: ## Live smoke test against the demo site: needs Claude API credentials + Chromium
	$(UV) run pytest -m live $(PYTEST_ARGS)

check: lint format-check typecheck test ## All offline gates: lint, format, types, unit tests

ci: UV_SYNC_FLAGS := --locked
ci: install check validate ## What CI runs: locked install, all offline gates, Gherkin validation

# --- Framework CLI -----------------------------------------------------------

validate: ## Validate Gherkin only, no browser or model (FEATURES=path)
	$(UV) run agentic-e2e validate $(FEATURES)

run: ## Run features end-to-end; needs ANTHROPIC_API_KEY (FEATURES=path OUT=dir BASE_URL=url)
	$(UV) run agentic-e2e run $(FEATURES) --out $(OUT) $(if $(BASE_URL),--base-url $(BASE_URL))

summarize: ## Aggregate per-feature reports into a suite summary (OUT=dir)
	$(UV) run agentic-e2e summarize $(OUT)

demo-site: ## Serve the demo app for features/refund.feature on http://127.0.0.1:$(DEMO_PORT)/
	$(UV) run python -m http.server $(DEMO_PORT) --bind 127.0.0.1 --directory examples/demo-site

# --- Housekeeping ------------------------------------------------------------

clean: ## Remove caches and build outputs (keeps artifacts)
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist
	find . -type d -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +

clean-artifacts: ## Delete run artifacts in OUT
	rm -rf -- "$(OUT)"
