#!/usr/bin/env bash
# Install epitaph in a clean arm64 Debian 13 (trixie) container and prove the install is
# idempotent (11 item 6). Runs on the laptop: podman plus qemu-user-static
# emulate the Pi's aarch64; nothing touches the Pi.
#
#   tools/test_install_arm64.sh [--image IMAGE] [--keep]
#   make install-test-arm64
#
#   --image IMAGE   the base image (default docker.io/library/debian:trixie, arm64 variant)
#   --keep          keep the stopped container (epitaph-install-test) to commit and look inside
#
# In the container, as on a fresh Raspberry Pi OS: python3 and systemd are present (not
# running), user `pi` exists, the source is copied to /opt/epitaph/src owned by pi (what
# tools/pi_deploy.sh does), and llama-server is a stub (tools/build_llamacpp.sh takes about an
# hour natively; install.sh only checks that the binary is there). Then:
#
#   1. deploy/install.sh --container --enable      installs everything; must not fail
#   2. deploy/install.sh --container --enable      must report "changed: 0"
#   3. deploy/install.sh --container --check       must exit 0 (no drift)
#   4. checks: the venv imports epitaph and pygame and runs the CLI as pi; the units are in
#      /etc/systemd/system for pi and enabled; the helpers are root 0755 and refuse bad
#      input; the sudoers drop-ins are root 0440, pass visudo -cf, and give pi exactly the
#      two helpers
#
# --container skips only what needs a booted systemd or the Pi itself (deploy/install.sh).
# About 10-12 minutes on a 12-thread laptop (584-713 s measured; emulated apt and pip take
# almost all of it), so it is not part of `make check` or CI. Exit 0 when every step passes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE=docker.io/library/debian:trixie
KEEP=0
NAME=epitaph-install-test
while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="${2:?--image needs a name}"; shift ;;
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,/^set -euo/{/^#/p}' "$0"; exit 0 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done

command -v podman >/dev/null || { echo "podman missing (apt install podman)" >&2; exit 2; }
if [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
  echo "no qemu-aarch64 binfmt handler (apt install qemu-user-static)" >&2; exit 2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# The tree as tools/pi_deploy.sh ships it: tracked and untracked files, minus .gitignore.
(cd "$ROOT" && git ls-files -co --exclude-standard -z \
  | xargs -0 -I{} sh -c 'test -e "{}" && printf "%s\0" "{}"' \
  | tar --null -T - -cf "$WORK/src.tar")

cat > "$WORK/inner.sh" <<'INNER'
#!/bin/bash
set -euo pipefail
pass() { printf '  PASS  %s\n' "$1"; }
die() { printf '  FAIL  %s\n' "$1"; exit 1; }

echo "== base: $(uname -m), $(. /etc/os-release && echo "$PRETTY_NAME")"
[ "$(uname -m)" = aarch64 ] || die "not aarch64"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 systemd >/dev/null
useradd --create-home --user-group --shell /bin/bash pi
install -d -o pi -g pi -m 0755 /opt/epitaph /opt/epitaph/src
tar -xf /mnt/in/src.tar -C /opt/epitaph/src
chown -R pi:pi /opt/epitaph/src
install -d -o pi -g pi /home/pi/llama.cpp/build/bin
printf '#!/bin/sh\necho "llama-server stub"\n' > /home/pi/llama.cpp/build/bin/llama-server
chmod 0755 /home/pi/llama.cpp/build/bin/llama-server
chown pi:pi /home/pi/llama.cpp/build/bin/llama-server

run() {  # run N ARGS...: the install, its output kept in /tmp/install-N.log
  local n="$1"; shift
  echo "== install run $n: $*"
  set +e; /opt/epitaph/src/deploy/install.sh "$@" > "/tmp/install-$n.log" 2>&1; rc=$?; set -e
  sed 's/^/    /' "/tmp/install-$n.log"
  return "$rc"
}
t0=$SECONDS
run 1 --container --enable || die "first install failed"
pass "first install ($((SECONDS - t0)) s)"
t0=$SECONDS
run 2 --container --enable || die "second install failed"
grep -qx 'changed: 0' /tmp/install-2.log || die "second install changed something"
pass "second install: changed: 0 ($((SECONDS - t0)) s)"
run 3 --container --check || die "--check reports drift after an install"
pass "--check: no drift"

echo "== checks"
owner() { stat -c '%U:%G %a' "$1"; }
# the venv
as_pi() { runuser -u pi -- "$@"; }
[ "$(stat -c %U /opt/epitaph/venv)" = pi ] || die "venv not owned by pi"
as_pi /opt/epitaph/venv/bin/python -c 'import epitaph, pygame' || die "venv cannot import epitaph, pygame"
as_pi /opt/epitaph/venv/bin/python -c 'import epitaph, sys; assert epitaph.__file__.startswith("/opt/epitaph/src/"), epitaph.__file__' \
  || die "epitaph not installed editable from /opt/epitaph/src"
(cd / && as_pi /opt/epitaph/venv/bin/epitaph --help >/dev/null) || die "epitaph --help fails"
pass "venv: epitaph (editable) and pygame import; the CLI runs as pi"
# state
for d in /var/lib/epitaph /var/lib/epitaph/models; do
  [ "$(owner "$d")" = "pi:pi 755" ] || die "$d is $(owner "$d")"
done
pass "state dirs owned by pi"
# units
for u in epitaph-controller.service epitaph-display.service; do
  f="/etc/systemd/system/$u"
  [ "$(owner "$f")" = "root:root 644" ] || die "$f is $(owner "$f")"
  grep -qx 'User=pi' "$f" || die "$f has no User=pi"
  ! grep -q '@USER@' "$f" || die "$f still has @USER@"
  [ -L "/etc/systemd/system/multi-user.target.wants/$u" ] || die "$u not enabled"
  systemctl is-enabled --quiet "$u" || die "systemctl is-enabled $u"
done
pass "units in /etc/systemd/system, User=pi, enabled for multi-user.target"
# helpers
for h in epitaph-clock epitaph-netblock; do
  f="/usr/local/sbin/$h"
  [ "$(owner "$f")" = "root:root 755" ] || die "$f is $(owner "$f")"
  cmp -s "$f" "/opt/epitaph/src/deploy/sbin/$h" || die "$f differs from the source"
done
set +e; /usr/local/sbin/epitaph-clock 5000 2>/dev/null; rc=$?; set -e
[ "$rc" = 2 ] || die "epitaph-clock 5000 exited $rc, not 2"
set +e; /usr/local/sbin/epitaph-netblock add /etc 2>/dev/null; rc=$?; set -e
[ "$rc" != 0 ] || die "epitaph-netblock accepted a foreign path"
pass "helpers root 0755, identical to the source, refuse bad input"
# sudoers
for f in /etc/sudoers.d/020_epitaph-clock /etc/sudoers.d/021_epitaph-netblock; do
  [ "$(owner "$f")" = "root:root 440" ] || die "$f is $(owner "$f")"
  visudo -cf "$f" >/dev/null || die "visudo -cf $f"
done
visudo -c >/dev/null || die "visudo -c (the whole configuration)"
# What sudo would let pi run, asked as root (sudo -l -U): setuid does not survive qemu-user, so
# pi cannot run sudo itself in the container.
may() { sudo -n -l -U pi "$@" >/dev/null 2>&1; }
may /usr/local/sbin/epitaph-clock reset || die "pi may not run epitaph-clock reset"
may /usr/local/sbin/epitaph-clock 1200 || die "pi may not run epitaph-clock 1200"
may /usr/local/sbin/epitaph-netblock status || die "pi may not run epitaph-netblock status"
may /usr/local/sbin/epitaph-netblock add system.slice/epitaph-controller.service/creature \
  || die "pi may not run epitaph-netblock add <creature>"
! may /usr/local/sbin/epitaph-clock 1200 extra || die "pi may run epitaph-clock with extra arguments"
! may /usr/local/sbin/epitaph-netblock add /etc || die "pi may run epitaph-netblock on a foreign path"
! may /bin/sh || die "pi may run /bin/sh as root"
pass "sudoers drop-ins root 0440, visudo -cf clean, pi gets exactly the two helpers"
echo "ALL PASS"
INNER
chmod 0755 "$WORK/inner.sh"

echo "== $IMAGE (linux/arm64) under qemu, source from $ROOT"
start=$SECONDS
podman pull -q --arch arm64 "$IMAGE" >/dev/null
podman rm -f "$NAME" >/dev/null 2>&1 || true
rm_flag=(--rm); [ "$KEEP" = 1 ] && rm_flag=()
set +e
podman run "${rm_flag[@]}" --name "$NAME" --arch arm64 -v "$WORK:/mnt/in:ro,Z" "$IMAGE" /mnt/in/inner.sh
rc=$?
set -e
echo "== $((SECONDS - start)) s in all; exit $rc"
if [ "$KEEP" = 1 ]; then
  echo "   kept: podman commit $NAME $NAME:last && podman run -it --rm --arch arm64 $NAME:last bash"
  echo "   then: podman rm $NAME && podman rmi $NAME:last"
fi
exit "$rc"
