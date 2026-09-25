# The single place that names the real commands.
#
# CI calls these targets rather than spelling the commands out itself, so that
# changing how something is checked is an edit here — not an edit to a file under
# .github/workflows/, which needs a token scope the automation account does not have.

.DEFAULT_GOAL := help
.PHONY: help format quality format-check lint typecheck test test-core test-all \
        test-cov codec-floor docs docs-serve build release-check clean install

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
#
# `--with .` installs matview itself. --no-project does not, and under the src/ layout
# the working directory is no longer incidentally importable the way a flat layout was —
# which is exactly how this job broke when the package moved.
#
# --refresh-package is load-bearing, not belt-and-braces: uv caches the wheel it builds
# from `.` and will happily reinstall a stale one, so without it this job silently
# verified an older matview. A job whose whole purpose is catching drift must not be able
# to test yesterday's code. tests/test_codecs.py now also asserts that the installed
# codec set matches the checked-in fixture, which fails loudly if this ever regresses.
#
# Caveat worth knowing: run from this directory, uv discovers the project's .venv and
# layers it in, editable .pth and all, so a *local* run is not a true isolation check and
# can mask a missing dependency. CI has no .venv, so it is the authority for this one.
codec-floor:  ## Check the golden codec bytes under the oldest supported libraries
	uv run --no-project --refresh-package matview --with . \
		--with 'polars==1.20.0' --with 'pyarrow==16.1.0' --with 'numpy==1.26.4' \
		--with 'pandas==2.2.3' --with 'pytest>=8.0' \
		python -m pytest tests/test_codecs.py -q

check: quality test-core test-all codec-floor  ## Everything CI runs, locally

docs:  ## Build the documentation site (strict)
	uv run --extra docs mkdocs build --strict

docs-serve:  ## Serve the documentation site with live reload
	uv run --extra docs mkdocs serve

build:  ## Build sdist and wheel
	rm -rf dist
	uv build

# What a published release is checked against before it exists. The suite proves the code
# works from the working tree; this proves the *distribution* works: that its metadata is
# valid on an index, that the wheel imports with nothing else installed, that py.typed
# actually shipped, and that the console script resolves. Each of those has a failure mode
# the test suite cannot see, because the tests import an editable install.
release-check: build  ## Build, validate the metadata and smoke-test the wheel in isolation
	uvx twine check --strict dist/*
	@wheel=$$(ls dist/*.whl) ; set -e ; \
	python -c "import sys, zipfile; names = zipfile.ZipFile(sys.argv[1]).namelist(); \
	sys.exit(0) if 'matview/py.typed' in names else sys.exit('py.typed missing from ' + sys.argv[1])" $$wheel ; \
	uv run --no-project --isolated --with $$wheel python -c 'import matview; print("import ok:", matview.__version__)' ; \
	uv run --no-project --isolated --with $$wheel matview --version

clean:  ## Remove build artifacts and caches
	rm -rf dist build site .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.egg-info' -type d -prune -exec rm -rf {} +
