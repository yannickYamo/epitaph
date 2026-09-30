PY      := .venv/bin/python
PROFILE ?= pi4/compressed-2700
PROFILES_PI4 := pi4/default pi4/smoke-300 pi4/skeleton-1200 pi4/compressed-2700 pi4/unbounded

.PHONY: check lint type test sim estimate venv pi-deploy pi-smoke pi-life

venv:
	python3 -m venv .venv && $(PY) -m pip install -q -e '.[dev,display]'

lint:
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

type:
	$(PY) -m pyright

test:
	$(PY) -m pytest -q --cov --cov-report=term-missing:skip-covered

sim:
	$(PY) -m epitaph sim --profile pi4/default --hardware pi4-4gb --lives 2 --quiet

estimate:
	@for p in $(PROFILES_PI4); do $(PY) -m epitaph estimate --profile $$p --hardware pi4-4gb || exit 1; done

# The merge gate (BUILD_PLAN 8.3).
check: lint type test sim estimate

pi-deploy:
	tools/pi_lock.sh run $${AGENT:-L} 15 -- tools/pi_deploy.sh

pi-smoke:
	tools/pi_lock.sh run $${AGENT:-L} 20 -- tools/smoke_pi.sh

pi-life:
	tools/pi_lock.sh run $${AGENT:-L} 120 -- tools/pi_life.sh $(PROFILE)
