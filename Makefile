.PHONY: install install-browsers test test-unit test-fast lint lint-fix typecheck clean dist

install:  ## editable install + dev deps
	pip install -e ".[dev]"

install-browsers:  ## playwright chromium
	python -m playwright install chromium

test:  ## full suite
	python -m pytest tests/ -q --timeout=300

test-unit:  ## fast offline-only subset (no daemon/LLM)
	python -m pytest tests/test_app_config.py tests/test_ssrf.py tests/test_providers.py \
		tests/test_integrations.py tests/test_token_budget.py tests/test_extractor.py \
		tests/test_circuit_metering.py -q --timeout=120

test-fast:  ## parallel full suite (needs pytest-xdist)
	python -m pytest tests/ -q -n auto --timeout=300

lint:  ## ruff check (production code; tests linted separately, see docs)
	ruff check src/

lint-fix:  ## ruff check --fix (safe fixes only)
	ruff check src/ --fix

clean:
	rm -rf .pytest_cache build dist src/*.egg-info

dist: clean  ## build sdist + wheel
	python -m build
