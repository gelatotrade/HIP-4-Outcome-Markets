.PHONY: help install install-dev test smoke lint typecheck format \
        run dashboard render-gif executor-build executor-test \
        docker-build docker-up docker-down clean

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip
CARGO  ?= cargo

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---- Python ----------------------------------------------------------------

install: ## Install runtime dependencies
	$(PIP) install -r requirements.txt

install-dev: install ## Install dev tools (pytest, ruff, mypy)
	$(PIP) install -e ".[dev]"

test: ## Run pytest suite
	$(PYTHON) -m pytest

smoke: ## Run end-to-end smoke against the simulator
	$(PYTHON) scripts/smoke.py

lint: ## ruff lint
	ruff check src scripts tests

format: ## ruff format
	ruff format src scripts tests

typecheck: ## mypy
	mypy src

run: dashboard ## alias for dashboard
dashboard: ## Run the Dash app locally (no Docker)
	$(PYTHON) -m src.app

render-gif: ## Render docs/surface.gif from the simulator
	$(PYTHON) scripts/render_animated_gif.py --frames 36 --fps 6 --notional 10000

# ---- Rust executor ---------------------------------------------------------

executor-build: ## Build the Rust execution daemon (release)
	cd executor && $(CARGO) build --release

executor-test: ## Run cargo tests
	cd executor && $(CARGO) test

executor-run: ## Run executor against api.hyperliquid.xyz (env vars required)
	cd executor && $(CARGO) run --release

# ---- Docker ----------------------------------------------------------------

docker-build: ## Build both container images
	docker compose build

docker-up: ## Start dashboard + executor (detached)
	docker compose up -d

docker-down: ## Tear down compose stack
	docker compose down

# ---- Misc ------------------------------------------------------------------

clean:
	rm -rf .pytest_cache __pycache__ src/__pycache__ scripts/__pycache__ \
	       .ruff_cache .mypy_cache executor/target
