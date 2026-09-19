# The single place that names the real commands.
#
# CI calls these targets rather than spelling the commands out itself, so that
# changing how something is checked is an edit here — not an edit to a file under
# .github/workflows/, which needs a token scope the automation account does not have.

.DEFAULT_GOAL := help
.PHONY: help format quality format-check lint typecheck test test-core test-all \
        test-cov codec-floor docs docs-serve build clean install

# The package path to lint and typecheck. Under src/ so that tests import the
# *installed* package rather than the working directory: a packaging mistake — a missing
# py.typed, an unshipped subpackage — then fails here rather than only for a user.
PACKAGE := src/matview

# `--exact` matters: without it uv leaves previously synced extras in the venv, and a
# run meant to prove the core works without them silently tests the opposite.
CORE := uv run --extra dev --exact
ALL  := uv run --all-extras

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package with every extra, for development
	uv sync --all-extras

format:  ## Auto-format
	$(ALL) ruff format .
	$(ALL) ruff check --fix .

format-check:  ## Check formatting without changing anything
	$(ALL) ruff format --check .

lint:  ## Lint
	$(ALL) ruff check .

typecheck:  ## Type check the package (strict)
	$(CORE) mypy $(PACKAGE)

quality: format-check lint typecheck  ## All static checks, in the order CI runs them

test: test-all  ## Alias for the full-extras suite

test-all:  ## Run the suite with every codec extra installed
	$(ALL) pytest

test-core:  ## Run the suite with NO codec extras, which must also pass
	$(CORE) pytest

test-cov:  ## Run the suite with coverage
	$(ALL) pytest --cov=matview --cov-report=term-missing

# Byte-determinism has to hold across library versions, not just across runs on one
# machine. A codec that drifts does not error: every artifact stored under it simply
# stops being found, and the cache looks permanently cold.
codec-floor:  ## Check the golden codec bytes under the oldest supported libraries
	uv run --no-project \
		--with 'polars==1.20.0' --with 'pyarrow==16.1.0' --with 'numpy==1.26.4' \
		--with 'pandas==2.2.3' \
		--with 'rfc8785>=0.1.4' --with 'pytest>=8.0' \
		python -m pytest tests/test_codecs.py -q

check: quality test-core test-all codec-floor  ## Everything CI runs, locally

docs:  ## Build the documentation site (strict)
	uv run --extra docs mkdocs build --strict

docs-serve:  ## Serve the documentation site with live reload
	uv run --extra docs mkdocs serve

build:  ## Build sdist and wheel
	uv build

clean:  ## Remove build artifacts and caches
	rm -rf dist build site .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.egg-info' -type d -prune -exec rm -rf {} +
