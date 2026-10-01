# PYTHONPATH makes every worktree test its own src/, not the editable install of main.
PY      := PYTHONPATH=$(CURDIR)/src $(CURDIR)/.venv/bin/python
PROFILE ?= pi4/default
PROFILES_PI4 := pi4/default pi4/smoke-300 pi4/skeleton-1200 pi4/compressed-2700 pi4/unbounded

.PHONY: check lint type test sim estimate venv faults pi-deploy pi-smoke pi-life pi-boot-check \
	pi-collect pi-faults

venv:
	python3 -m venv .venv && $(PY) -m pip install -q -e '.[dev,display]'

lint:
	$(PY) -m ruff check src tests tools
	$(PY) -m ruff format --check src tests tools

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

# The fault matrix rows the fakes inject (BUILD_PLAN 10.4; docs/GATES.md fault table). Part of
# `make test` too; this runs them alone.
faults:
	$(PY) -m pytest -q tests/faults

# Pi targets (BUILD_PLAN 8.3; docs/GATES.md G1). All run under the Pi lock, as AGENT (default L).
# The lives and the reports go to logs/pi/ (untracked).
pi-deploy:
	tools/pi_lock.sh run $${AGENT:-L} 15 -- tools/pi_deploy.sh

# One smoke-300 life, judged at level smoke (G1.4).
pi-smoke:
	tools/smoke_pi.sh --agent $${AGENT:-L}

# LIVES consecutive lives of PROFILE (default pi4/default, 1 life), each judged at the
# profile's level; with LIVES=2 the first one's next_birth is judged too (G1.1:
# PROFILE=pi4/skeleton-1200 LIVES=2; G2.3: PROFILE=pi4/default LIVES=3).
pi-life:
	tools/smoke_pi.sh --agent $${AGENT:-L} --profile $(PROFILE) --lives $${LIVES:-1}

# Reboot the Pi and check the headless boot (G1.3); REBOOT=0 checks the current boot only.
pi-boot-check:
	mkdir -p logs/pi
	tools/headless_boot_check.sh --agent $${AGENT:-L} $$([ "$${REBOOT:-1}" = 0 ] || echo --reboot) \
		--out logs/pi/boot-check-$$(date +%Y%m%d-%H%M%S).txt

# The newest LIVES (default 3) finished lives of PROFILE from the running service, copied to
# logs/pi/service-<stamp>/ and judged (read-only: no lock, the service keeps running).
# PROFILE=any takes lives of every profile.
pi-collect:
	tools/collect_lives.sh --lives $${LIVES:-3} $$([ "$(PROFILE)" = any ] || echo --profile $(PROFILE))

# The fault matrix on the Pi (G2.2) through tools/fault_pi.sh, in one hold of the Pi lock; the
# table goes to logs/pi/faults-<stamp>.md. ROWS=a,b runs only those (tools/fault_matrix_pi.sh --list).
pi-faults:
	tools/fault_matrix_pi.sh --agent $${AGENT:-L} $${ROWS:+--rows $$ROWS}
