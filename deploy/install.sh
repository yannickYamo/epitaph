#!/usr/bin/env bash
# Install epitaph on a Raspberry Pi from the deployed source (BUILD_PLAN 9 C5, C10). Idempotent:
# a second run changes nothing and says so ("changed: 0").
#
#   sudo deploy/install.sh [--user USER] [--enable] [--no-selftest] [--check] [--container]
#
#   --user USER     the service user (default: pi; the Pi is dedicated to the piece and the
#                   state in /var/lib/epitaph already belongs to it)
#   --enable        also enable the units at boot (default: install them disabled)
#   --no-selftest   skip `epitaph selftest` at the end (the delegated cgroup checks)
#   --check         report what would change, change nothing (exit 1 if anything would)
#   --container     a container with no running systemd and no Pi hardware (the arm64 install
#                   test, tools/test_install_arm64.sh): files, venv and enable links are
#                   installed exactly as on the Pi; skipped are only the steps that need a
#                   booted systemd or the real machine (daemon-reload, the cgroup v2
#                   controllers, the hardware watchdog, selftest)
#
# What it manages:
#   apt packages                            python3-venv, nftables and sudo, installed only
#                                           when missing (a fresh Raspberry Pi OS may lack them)
#   /opt/epitaph/venv                       python venv, the package installed (editable) from
#                                           /opt/epitaph/src, owned by the service user
#   /usr/local/sbin/epitaph-clock           the CPU clock helper (root, 0755; ADR-025)
#   /etc/sudoers.d/020_epitaph-clock        the service user may run exactly that helper
#                                           (0440, checked with visudo -cf)
#   /usr/local/sbin/epitaph-netblock        the creature's network block (root, 0755; ADR-005):
#                                           an nftables rule per creature cgroup, loaded by
#                                           the body at every controller start
#   /etc/sudoers.d/021_epitaph-netblock     the service user may run exactly that helper
#   /etc/systemd/system/epitaph-{controller,display}.service
#   /var/lib/epitaph                        state dir (models in models/), owned by the user
# and checks, without changing them: cgroup v2 with memory, cpu and io; the hardware watchdog
# (RuntimeWatchdogUSec, the OS default); the llama-server binary; systemd-analyze verify.
# It never touches secrets, the network configuration or the boot configuration (that is
# pi_bootstrap.sh).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
cd /  # the service user must be able to read the working directory of what it runs
USER_NAME=pi; ENABLE=0; SELFTEST=1; MODE=apply; CONTAINER=0
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="${2:?--user needs a name}"; shift ;;
    --enable) ENABLE=1 ;;
    --no-selftest) SELFTEST=0 ;;
    --check) MODE=check ;;
    --container) CONTAINER=1; SELFTEST=0 ;;
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
# helper name -> sudoers drop-in number (deploy/sbin/<name>, deploy/sudoers/<name>)
HELPERS=(epitaph-clock:020 epitaph-netblock:021)
UNIT_DIR=/etc/systemd/system
UNITS=(epitaph-controller.service epitaph-display.service)
EXTRAS="${EPITAPH_EXTRAS:-display}"
# Debian packages the install needs; Raspberry Pi OS ships python3 and systemd.
PACKAGES=(python3-venv nftables sudo)

# The units read config/ from $PREFIX/src (EPITAPH_CONFIG_DIR) and the venv runs the code in
# place, so the source must be there (or be what $PREFIX/src links to).
if [ "$(realpath "$SRC")" != "$(realpath -m "$PREFIX/src")" ]; then
  echo "the source must be at $PREFIX/src (the units run it from there), not $SRC" >&2
  exit 2
fi

CHANGED=0; FAILED=0; RELOAD=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

ok()     { printf '  ok       %s\n' "$1"; }
change() { printf '  changed  %s\n' "$1"; CHANGED=$((CHANGED + 1)); }
would()  { printf '  drift    %s\n' "$1"; CHANGED=$((CHANGED + 1)); }
fail()   { printf '  FAIL     %s\n' "$1"; FAILED=$((FAILED + 1)); }
skip()   { printf '  skip     %s (--container)\n' "$1"; }
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

# --- packages --------------------------------------------------------------------------------
missing=()
for p in "${PACKAGES[@]}"; do
  if dpkg-query -W -f '${Status}' "$p" 2>/dev/null | grep -q 'ok installed'; then ok "package $p"
  else missing+=("$p"); fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  if [ "$MODE" = check ]; then
    for p in "${missing[@]}"; do would "package $p"; done
  else
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}" >/dev/null
    for p in "${missing[@]}"; do change "package $p"; done
  fi
fi

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
  spec="$SRC"; [ -n "$EXTRAS" ] && spec="${SRC}[$EXTRAS]"
  as_user "$VENV/bin/pip" install --quiet --disable-pip-version-check --editable "$spec"
  printf '%s\n' "$want_stamp" | as_user tee "$STAMP" >/dev/null
  change "$VENV (pip install -e '$spec')"
fi

# --- the privileged helpers and their sudoers rules (ADR-025, ADR-005) ----------------------
for entry in "${HELPERS[@]}"; do
  name="${entry%%:*}"; num="${entry##*:}"
  install_file "$SRC/deploy/sbin/$name" "/usr/local/sbin/$name" 0755 root:root || true
  sed "s/@USER@/$USER_NAME/g" "$SRC/deploy/sudoers/$name" > "$TMP/sudoers-$name"
  if ! visudo -cqf "$TMP/sudoers-$name"; then
    fail "sudoers drop-in for $name does not parse (visudo -cf); not installed"
  else
    install_file "$TMP/sudoers-$name" "/etc/sudoers.d/${num}_$name" 0440 root:root || true
  fi
done
# (nft --version opens a netlink socket, which qemu-user lacks; dpkg knows the version too)
if command -v nft >/dev/null 2>&1; then
  ok "nft ($(nft --version 2>/dev/null || dpkg-query -W -f 'nftables ${Version}' nftables 2>/dev/null))"
else fail "nft missing (apt install nftables): the creature's network cannot be blocked"; fi

# --- systemd units ---------------------------------------------------------------------------
for u in "${UNITS[@]}"; do
  sed "s/@USER@/$USER_NAME/g" "$SRC/deploy/systemd/$u" > "$TMP/$u"
  if install_file "$TMP/$u" "$UNIT_DIR/$u" 0644 root:root; then RELOAD=1; fi
done
if [ "$RELOAD" = 1 ]; then
  if [ "$CONTAINER" = 1 ]; then skip "systemctl daemon-reload"
  else systemctl daemon-reload; echo "  (systemctl daemon-reload)"; fi
fi
if [ "$ENABLE" = 1 ]; then
  for u in "${UNITS[@]}"; do
    if systemctl is-enabled --quiet "$u"; then ok "$u enabled"
    elif [ "$MODE" = check ]; then would "$u enable"
    else systemctl enable --quiet "$u"; change "$u enabled"; fi
  done
fi

# --- checks (nothing changes below) -------------------------------------------------------
echo "== checks"
if [ "$CONTAINER" = 1 ]; then
  skip "cgroup v2 controllers memory cpu io"
  skip "hardware watchdog"
else
  ctrl="$(cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || true)"
  for c in memory cpu io; do
    case " $ctrl " in *" $c "*) ok "cgroup v2 controller $c" ;; *) fail "cgroup v2 controller $c missing ($ctrl)" ;; esac
  done
  wd="$(systemctl show -p RuntimeWatchdogUSec --value)"
  if [ -n "$wd" ] && [ "$wd" != 0 ]; then ok "hardware watchdog RuntimeWatchdogUSec=$wd"
  else fail "hardware watchdog off (RuntimeWatchdogUSec=$wd)"; fi
fi
LLAMA="$USER_HOME/llama.cpp/build/bin/llama-server"
if [ -x "$LLAMA" ]; then ok "$LLAMA"; else fail "$LLAMA missing (tools/build_llamacpp.sh --pi)"; fi
if [ "$MODE" = apply ]; then
  # systemd-analyze verify reads the unit files offline: it runs in the container too.
  if out="$(systemd-analyze verify "${UNITS[@]/#/$UNIT_DIR/}" 2>&1)" && [ -z "$out" ]; then
    ok "systemd-analyze verify"
  else
    fail "systemd-analyze verify: $out"
  fi
  if [ "$CONTAINER" = 1 ]; then
    skip "selftest"
  elif [ "$SELFTEST" = 1 ]; then
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
