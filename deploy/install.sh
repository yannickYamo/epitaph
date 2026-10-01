#!/usr/bin/env bash
# Install epitaph on a Raspberry Pi from the deployed source (BUILD_PLAN 9 C5, C10). Idempotent:
# a second run changes nothing and says so ("changed: 0").
#
#   sudo deploy/install.sh [--user USER] [--enable] [--no-selftest] [--check]
#
#   --user USER     the service user (default: pi; the Pi is dedicated to the piece and the
#                   state in /var/lib/epitaph already belongs to it)
#   --enable        also enable the units at boot (default: install them disabled)
#   --no-selftest   skip `epitaph selftest` at the end (the delegated cgroup checks)
#   --check         report what would change, change nothing (exit 1 if anything would)
#
# What it manages:
#   /opt/epitaph/venv                       python venv, the package installed (editable) from
#                                           /opt/epitaph/src, owned by the service user
#   /usr/local/sbin/epitaph-clock           the CPU clock helper (root, 0755; ADR-025)
#   /etc/sudoers.d/020_epitaph-clock        the service user may run exactly that helper
#                                           (0440, checked with visudo -cf)
#   /etc/systemd/system/epitaph-{controller,display}.service
#   /var/lib/epitaph                        state dir (models in models/), owned by the user
# and checks, without changing them: cgroup v2 with memory, cpu and io; the hardware watchdog
# (RuntimeWatchdogUSec, the OS default); the llama-server binary; systemd-analyze verify.
# It never touches secrets, the network or the boot configuration (that is pi_bootstrap.sh).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
cd /  # the service user must be able to read the working directory of what it runs
USER_NAME=pi; ENABLE=0; SELFTEST=1; MODE=apply
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="${2:?--user needs a name}"; shift ;;
    --enable) ENABLE=1 ;;
    --no-selftest) SELFTEST=0 ;;
    --check) MODE=check ;;
    -h|--help) sed -n '2,/^set -euo/{/^#/p}' "$0"; exit 0 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done

[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0 $*" >&2; exit 2; }
id "$USER_NAME" >/dev/null 2>&1 || { echo "no user $USER_NAME" >&2; exit 2; }
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"

PREFIX=/opt/epitaph
VENV="$PREFIX/venv"
STATE=/var/lib/epitaph
HELPER=/usr/local/sbin/epitaph-clock
SUDOERS=/etc/sudoers.d/020_epitaph-clock
UNIT_DIR=/etc/systemd/system
UNITS=(epitaph-controller.service epitaph-display.service)
EXTRAS="${EPITAPH_EXTRAS:-display}"

CHANGED=0; FAILED=0; RELOAD=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

ok()     { printf '  ok       %s\n' "$1"; }
change() { printf '  changed  %s\n' "$1"; CHANGED=$((CHANGED + 1)); }
would()  { printf '  drift    %s\n' "$1"; CHANGED=$((CHANGED + 1)); }
fail()   { printf '  FAIL     %s\n' "$1"; FAILED=$((FAILED + 1)); }
as_user() { runuser -u "$USER_NAME" -- "$@"; }

# install_file SRC DEST MODE OWNER: copy only when content, mode or owner differ.
install_file() {
  local src="$1" dest="$2" mode="$3" owner="$4"
  if [ -f "$dest" ] && cmp -s "$src" "$dest" \
     && [ "$(stat -c '%a %U:%G' "$dest")" = "${mode#0} $owner" ]; then
    ok "$dest"; return 1
  fi
  if [ "$MODE" = check ]; then would "$dest"; return 1; fi
  install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$src" "$dest"
  change "$dest"
  return 0
}

echo "== epitaph install from $SRC (user $USER_NAME)"

# --- directories ---------------------------------------------------------------------------
for d in "$PREFIX" "$STATE" "$STATE/models"; do
  if [ -d "$d" ] && [ "$(stat -c %U "$d")" = "$USER_NAME" ]; then ok "$d"
  elif [ "$MODE" = check ]; then would "$d"
  else install -d -o "$USER_NAME" -g "$USER_NAME" -m 0755 "$d"; change "$d"; fi
done

# --- python venv and the package -------------------------------------------------------------
# Editable install: the code runs from /opt/epitaph/src, so config/ and bench/ are found next
# to it and a deploy (rsync) takes effect at the next start. The stamp records what the venv
# was built from; only a new pyproject.toml, extras or Python rebuilds the install.
STAMP="$VENV/.epitaph-stamp"
want_stamp="$(sha256sum "$SRC/pyproject.toml" | cut -d' ' -f1) extras=$EXTRAS $(python3 -V 2>&1)"
if [ -x "$VENV/bin/python" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$want_stamp" ] \
   && "$VENV/bin/python" -c 'import epitaph' 2>/dev/null; then
  ok "$VENV (epitaph installed from $SRC)"
elif [ "$MODE" = check ]; then
  would "$VENV"
else
  [ -x "$VENV/bin/python" ] || as_user python3 -m venv "$VENV"
  spec="$SRC"; [ -n "$EXTRAS" ] && spec="$SRC[$EXTRAS]"
  as_user "$VENV/bin/pip" install --quiet --disable-pip-version-check --editable "$spec"
  printf '%s\n' "$want_stamp" | as_user tee "$STAMP" >/dev/null
  change "$VENV (pip install -e '$spec')"
fi

# --- the clock helper and its sudoers rule (ADR-025) ---------------------------------------
install_file "$SRC/deploy/sbin/epitaph-clock" "$HELPER" 0755 root:root || true

sed "s/@USER@/$USER_NAME/g" "$SRC/deploy/sudoers/epitaph-clock" > "$TMP/sudoers"
if ! visudo -cqf "$TMP/sudoers"; then
  fail "sudoers drop-in does not parse (visudo -cf); not installed"
else
  install_file "$TMP/sudoers" "$SUDOERS" 0440 root:root || true
fi

# --- systemd units ---------------------------------------------------------------------------
for u in "${UNITS[@]}"; do
  sed "s/@USER@/$USER_NAME/g" "$SRC/deploy/systemd/$u" > "$TMP/$u"
  if install_file "$TMP/$u" "$UNIT_DIR/$u" 0644 root:root; then RELOAD=1; fi
done
if [ "$RELOAD" = 1 ]; then systemctl daemon-reload; echo "  (systemctl daemon-reload)"; fi
if [ "$ENABLE" = 1 ]; then
  for u in "${UNITS[@]}"; do
    if systemctl is-enabled --quiet "$u"; then ok "$u enabled"
    elif [ "$MODE" = check ]; then would "$u enable"
    else systemctl enable --quiet "$u"; change "$u enabled"; fi
  done
fi

# --- checks (nothing changes below) -------------------------------------------------------
echo "== checks"
ctrl="$(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || true)"
for c in memory cpu io; do
  case " $ctrl " in *" $c "*) ok "cgroup v2 controller $c" ;; *) fail "cgroup v2 controller $c missing ($ctrl)" ;; esac
done
wd="$(systemctl show -p RuntimeWatchdogUSec --value)"
if [ -n "$wd" ] && [ "$wd" != 0 ]; then ok "hardware watchdog RuntimeWatchdogUSec=$wd"
else fail "hardware watchdog off (RuntimeWatchdogUSec=$wd)"; fi
LLAMA="$USER_HOME/llama.cpp/build/bin/llama-server"
if [ -x "$LLAMA" ]; then ok "$LLAMA"; else fail "$LLAMA missing (tools/build_llamacpp.sh --pi)"; fi
if [ "$MODE" = apply ]; then
  if out="$(systemd-analyze verify "${UNITS[@]/#/$UNIT_DIR/}" 2>&1)" && [ -z "$out" ]; then
    ok "systemd-analyze verify"
  else
    fail "systemd-analyze verify: $out"
  fi
  if [ "$SELFTEST" = 1 ]; then
    echo "== epitaph selftest (transient Delegate=yes unit as $USER_NAME)"
    if (cd / && "$VENV/bin/epitaph" selftest --user "$USER_NAME" | sed 's/^/  /'; exit "${PIPESTATUS[0]}"); then
      ok "selftest"
    else
      fail "selftest"
    fi
  fi
fi

echo "changed: $CHANGED"
echo "failed: $FAILED"
[ "$FAILED" = 0 ] || exit 1
[ "$MODE" = check ] && [ "$CHANGED" != 0 ] && exit 1
exit 0
