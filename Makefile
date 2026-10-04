# PYTHONPATH makes every worktree test its own src/, not the editable install of main.
PY      := PYTHONPATH=$(CURDIR)/src $(CURDIR)/.venv/bin/python
PROFILE ?= pi4/default
PROFILES_PI4 := pi4/default pi4/default-reloads pi4/smoke-300 pi4/skeleton-1200 pi4/unbounded
# Pi 5: simulated and estimated only, on both overlays (BUILD_PLAN 3.2, 11.8).
PROFILES_PI5 := pi5/default pi5/skeleton-600 pi5/unbounded

.PHONY: check lint type test sim sim-profiles estimate badge venv faults pi-deploy pi-smoke pi-life \
	pi-boot-check pi-collect pi-faults install-test-arm64

venv:
	python3 -m venv .venv && $(PY) -m pip install -q -e '.[dev,display]'

lint:
	$(PY) -m ruff check src tests tools badge
	$(PY) -m ruff format --check src tests tools badge

type:
	$(PY) -m pyright

test:
	$(PY) -m pytest -q --cov --cov-report=term-missing:skip-covered

sim:
	$(PY) -m epitaph sim --profile pi4/default --hardware pi4-4gb --lives 2 --quiet

estimate:
	@for p in $(PROFILES_PI4); do $(PY) -m epitaph estimate --profile $$p --hardware pi4-4gb || exit 1; done
	@for hw in pi5-8gb pi5-16gb; do for p in $(PROFILES_PI5); do \
		$(PY) -m epitaph estimate --profile $$p --hardware $$hw || exit 1; done; done

# One simulated life of every profile that is not the installation's (11.8: pi4/unbounded and
# the Pi 5 profiles pass simulation).
sim-profiles:
	@for p in pi4/default-reloads pi4/smoke-300 pi4/skeleton-1200 pi4/unbounded; do \
		$(PY) -m epitaph sim --profile $$p --hardware pi4-4gb --quiet || exit 1; done
	@for hw in pi5-8gb pi5-16gb; do for p in $(PROFILES_PI5); do \
		$(PY) -m epitaph sim --profile $$p --hardware $$hw --quiet || exit 1; done; done

# The merge gate (BUILD_PLAN 8.3).
check: lint type test sim sim-profiles estimate badge

# The small-chip editions (badge/README.md): the int8 C engine against the float model, the
# readings' tokens and words on both ports, then whole lives on simulated boards (an ESP32 with
# a 300 KB heap, a board with nothing but a serial port, the Tufty badge), each checked: every
# loss read once, the last reading answered, a death by memory. Last, the terminal port for real.
badge:
	$(MAKE) -s -C badge/esp32 host/test_host
	$(PY) badge/tools/test_esp32.py
	$(PY) badge/tools/test_ports.py
	./badge/esp32/host/test_host life > /dev/null
	./badge/esp32/host/test_host bare > /dev/null
	$(PY) badge/tools/sim_badge.py --check > /dev/null
	$(PY) badge/tufty/epitaph/terminal.py 8 1 > /dev/null

# The fault matrix rows the fakes inject (BUILD_PLAN 10.4; docs/GATES.md fault table). Part of
# `make test` too; this runs them alone.
faults:
	$(PY) -m pytest -q tests/faults

# Pi targets (BUILD_PLAN 8.3; docs/GATES.md G1). All run under the Pi lock, as HOLDER (default L).
# The lives and the reports go to logs/pi/ (untracked).
pi-deploy:
	tools/pi_lock.sh run $${HOLDER:-make} 15 -- tools/pi_deploy.sh

# One smoke-300 life, judged at level smoke (G1.4).
pi-smoke:
	tools/smoke_pi.sh --holder $${HOLDER:-make}

# LIVES consecutive lives of PROFILE (default pi4/default, 1 life), each judged at the
# profile's level; with LIVES=2 the first one's next_birth is judged too (G1.1:
# PROFILE=pi4/skeleton-1200 LIVES=2; G2.3: PROFILE=pi4/default LIVES=3).
pi-life:
	tools/smoke_pi.sh --holder $${HOLDER:-make} --profile $(PROFILE) --lives $${LIVES:-1}

# Reboot the Pi and check the headless boot (G1.3); REBOOT=0 checks the current boot only.
pi-boot-check:
	mkdir -p logs/pi
	tools/headless_boot_check.sh --holder $${HOLDER:-make} $$([ "$${REBOOT:-1}" = 0 ] || echo --reboot) \
		--out logs/pi/boot-check-$$(date +%Y%m%d-%H%M%S).txt

# The newest LIVES (default 3) finished lives of PROFILE from the running service, copied to
# logs/pi/service-<stamp>/ and judged (read-only: no lock, the service keeps running).
# PROFILE=any takes lives of every profile.
pi-collect:
	tools/collect_lives.sh --lives $${LIVES:-3} $$([ "$(PROFILE)" = any ] || echo --profile $(PROFILE))

# The fault matrix on the Pi (G2.2) through tools/fault_pi.sh, in one hold of the Pi lock; the
# table goes to logs/pi/faults-<stamp>.md. ROWS=a,b runs only those (tools/fault_matrix_pi.sh --list).
pi-faults:
	tools/fault_matrix_pi.sh --holder $${HOLDER:-make} $${ROWS:+--rows $$ROWS}

# deploy/install.sh in a clean arm64 Debian trixie container under qemu (podman), run twice:
# the second run must change nothing (BUILD_PLAN 9 C10, 11 item 6). Laptop only, not CI: the
# emulated apt and pip make it slow (see tools/test_install_arm64.sh for the time).
install-test-arm64:
	tools/test_install_arm64.sh
