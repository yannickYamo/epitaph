#!/usr/bin/env bash
# Prepare a Raspberry Pi for epitaph: the Pi part of BUILD_PLAN 8.6 (step 0), idempotently.
#
#   tools/pi_bootstrap.sh [--check | --apply | --state] [--no-reboot] [host]    (laptop)
#   sudo tools/pi_bootstrap.sh --local [--check | --apply | --state]            (on the Pi)
#
#   --check   report drift, change nothing (exit 1 if anything would change)
#   --apply   fix drift (default); reboots only if a boot-time setting changed
#   --state   print the effective state of everything managed (diff two runs to prove a
#             run changed nothing)
#
# It never handles secrets. The Wi-Fi connection and the Pi password need a person at a
# real terminal; when they are missing it prints the command for Yannick and carries on.
# Run it under the Pi lock: tools/pi_lock.sh run C 15 -- tools/pi_bootstrap.sh --apply
# Every change it makes is also a row in docs/PI_CHANGES.md. Without a host it uses `pi`
# (Wi-Fi) and falls back to `pi-eth` (the cable) when `pi` does not answer (tools/pi_host.sh).
set -euo pipefail

MODE=apply; HOST="${PI_HOST:-}"; REBOOT_OK=1; LOCAL=0
for a in "$@"; do
  case "$a" in
    --check) MODE=check ;; --apply) MODE=apply ;; --state) MODE=state ;;
    --no-reboot) REBOOT_OK=0 ;; --local) LOCAL=1 ;;
    -h|--help) sed -n '2,/^set -euo/{/^#/p}' "$0"; exit 0 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) HOST="$a" ;;
  esac
done

if [ "$LOCAL" = 0 ]; then
  # ---- laptop side: ship the Pi part over SSH -----------------------------------------
  # shellcheck source=tools/pi_host.sh
  . "$(dirname "$0")/pi_host.sh"
  AUTO_HOST=0
  if [ -z "$HOST" ]; then
    AUTO_HOST=1
    HOST="$(pi_host)" || { echo "cannot reach the Pi with a key. Check ~/.ssh/config (docs/PI_FACTS.md)." >&2; exit 3; }
  fi
  ssh_() { ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 "$HOST" "$@"; }
  if ! ssh_ true; then
    echo "cannot reach $HOST with a key. Check ~/.ssh/config (docs/PI_FACTS.md)." >&2
    exit 3
  fi
  echo "== host: $HOST"
  if ! ssh_ sudo -n true 2>/dev/null; then
    cat >&2 <<'MSG'
sudo needs a password on the Pi, so this script cannot continue. Yannick, in your own
terminal (not through Claude Code), run this once and type the Pi password when asked:

  ssh -t pi-eth 'echo "pi ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/010_pi-nopasswd >/dev/null && sudo chmod 0440 /etc/sudoers.d/010_pi-nopasswd && sudo visudo -cf /etc/sudoers.d/010_pi-nopasswd'

then run this script again.
MSG
    exit 3
  fi
  remote() { sed -n '/^# ---- Pi side/,$p' "$0" | ssh_ "sudo -n bash -s -- --local --$MODE"; }
  set +e; remote; rc=$?; set -e
  if [ "$rc" = 10 ]; then
    if [ "$REBOOT_OK" = 1 ]; then
      echo "== rebooting $HOST for boot-time changes"
      ssh_ "sudo -n systemd-run --on-active=2 --quiet systemctl reboot" || true
      sleep 20
      for _ in $(seq 60); do
        # Wi-Fi may come up later than the cable (or not at all): pick again when auto.
        if [ "$AUTO_HOST" = 1 ]; then h="$(pi_host 2>/dev/null)" && { HOST="$h"; break; }
        else ssh_ true 2>/dev/null && break; fi
        sleep 5
      done
      ssh_ true || { echo "$HOST did not come back within 5 minutes" >&2; exit 1; }
      echo "== verifying after reboot"
      MODE=check; set +e; remote; rc=$?; set -e
    else
      echo "== a reboot is needed for boot-time changes (skipped: --no-reboot)"; rc=0
    fi
  fi
  exit "$rc"
fi

# ---- Pi side (everything below runs on the Pi as root) ----------------------------------
set -euo pipefail
MODE=apply
for a in "$@"; do case "$a" in --check) MODE=check ;; --apply) MODE=apply ;; --state) MODE=state ;; esac; done
[ "$(id -u)" = 0 ] || { echo "the Pi side must run as root" >&2; exit 2; }

USER_NAME=pi
HOSTNAME_WANT=epitaph
TZ_WANT=America/Los_Angeles
COUNTRY=US
CMDLINE=/boot/firmware/cmdline.txt
CMDLINE_TOKENS="cgroup_enable=memory cgroup_memory=1 consoleblank=0 cfg80211.ieee80211_regdom=$COUNTRY"
SUDOERS=/etc/sudoers.d/010_pi-nopasswd
SUDOERS_BODY="$USER_NAME ALL=(ALL) NOPASSWD: ALL"
JOURNALD=/etc/systemd/journald.conf.d/90-epitaph.conf
# Exact content matters: the check compares it byte for byte. 90- sorts after Raspberry Pi
# OS's 40-rpi-volatile-storage.conf, which would otherwise win.
JOURNALD_BODY='[Journal]
Storage=persistent
SystemMaxUse=200M'
SSHD=/etc/ssh/sshd_config.d/10-epitaph.conf
SSHD_BODY='# epitaph step 0 (BUILD_PLAN 8.6 step 10): keys everywhere; passwords only over the direct cable.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no

Match Address 10.42.0.0/24
    PasswordAuthentication yes'
AVAHI=/etc/avahi/avahi-daemon.conf
STATE_DIRS="/var/lib/epitaph /var/lib/epitaph/models"
PACKAGES="nftables"
STAMP="$(date +%Y%m%d-%H%M%S)"

CHANGED=(); DRIFT=(); MANUAL=(); FAILED=(); NEED_REBOOT=0

say() { printf '  %-22s %s\n' "$1" "$2"; }
# item <name> <check-fn> <apply-fn> <needs-reboot 0|1>
item() {
  local name="$1" check="$2" apply="$3" boot="$4"
  if "$check"; then say "$name" ok; return; fi
  if [ "$MODE" = check ]; then say "$name" DRIFT; DRIFT+=("$name"); return; fi
  if "$apply" && "$check"; then
    say "$name" changed; CHANGED+=("$name"); [ "$boot" = 1 ] && NEED_REBOOT=1
  else
    say "$name" FAILED; FAILED+=("$name")
  fi
  return 0
}
manual() { say "$1" "MANUAL"; MANUAL+=("$2"); }
backup() { [ -e "$1" ] && cp -a "$1" "$1.bak-bootstrap-$STAMP"; return 0; }
# Write a file only when its content differs; validate with an optional command first.
put_file() {  # <path> <mode> <content> [validator]
  local tmp; tmp="$(mktemp)"; printf '%s\n' "$3" > "$tmp"
  if [ -n "${4:-}" ] && ! $4 "$tmp" >/dev/null; then rm -f "$tmp"; return 1; fi
  install -o root -g root -m "$2" "$tmp" "$1"; rm -f "$tmp"; sync
}
same_file() {  # <path> <mode> <content>
  [ -f "$1" ] && [ "$(stat -c %a:%U:%G "$1")" = "$2:root:root" ] \
    && [ "$(cat "$1")" = "$3" ]
}
nm_get() { nmcli -g "$2" connection show "$1" 2>/dev/null; }
wifi_conns() { nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"{print $1}'; }
eth_conns() { nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-3-ethernet"{print $1}'; }

# --- sudo --------------------------------------------------------------------------------
chk_sudoers() { same_file "$SUDOERS" 440 "$SUDOERS_BODY"; }
fix_sudoers() { put_file "$SUDOERS" 0440 "$SUDOERS_BODY" "visudo -cqf"; }

# --- Wi-Fi country, radio, power save ----------------------------------------------------
chk_country() {
  [ "$(raspi-config nonint get_wifi_country 2>/dev/null)" = "$COUNTRY" ] \
    && ! grep -q "Soft blocked: yes" <<<"$(rfkill list wifi 2>/dev/null)"
}
fix_country() { raspi-config nonint do_wifi_country "$COUNTRY" && rfkill unblock wifi; }

chk_wifi_settings() {
  local c ok=0
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    case "$(nm_get "$c" 802-11-wireless.powersave)" in 2|disable) ;; *) ok=1 ;; esac
    [ "$(nm_get "$c" connection.autoconnect-priority)" = "10" ] || ok=1
  done < <(wifi_conns)
  return "$ok"
}
fix_wifi_settings() {
  local c
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    nmcli connection modify "$c" 802-11-wireless.powersave 2 connection.autoconnect-priority 10
  done < <(wifi_conns)
}

# --- the cable is maintenance-only --------------------------------------------------------
chk_eth() {
  local c ok=0
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    [ "$(nm_get "$c" ipv4.never-default)" = "yes" ] || ok=1
    [ "$(nm_get "$c" ipv6.never-default)" = "yes" ] || ok=1
  done < <(eth_conns)
  return "$ok"
}
fix_eth() {
  local c
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    nmcli connection modify "$c" ipv4.never-default yes ipv6.never-default yes
    nmcli device reapply eth0 >/dev/null 2>&1 || true
  done < <(eth_conns)
}

# --- cloud-init off after first boot, credentials out of user-data -----------------------
chk_cloudinit() { [ -e /etc/cloud/cloud-init.disabled ] || [ ! -d /etc/cloud ]; }
fix_cloudinit() {
  if grep -q running <<<"$(cloud-init status 2>/dev/null)"; then
    echo "cloud-init is still running its first boot; try again later" >&2; return 1
  fi
  touch /etc/cloud/cloud-init.disabled
}
USERDATA=/boot/firmware/user-data
# A password hash left in user-data is a credential on the FAT partition. Only its presence
# is checked; the value is never printed.
CRED_RE='^\s*(passwd|hashed_passwd|plain_text_passwd|password):\s*[^[:space:]]'
chk_userdata() {
  [ ! -f "$USERDATA" ] || ! grep -E "$CRED_RE" "$USERDATA" | grep -vq 'REMOVED-after-first-boot'
}
fix_userdata() {
  install -m 0600 "$USERDATA" "/root/user-data.bak-bootstrap-$STAMP"
  sed -Ei 's/^(\s*(passwd|hashed_passwd|plain_text_passwd|password):\s*).*/\1REMOVED-after-first-boot/' "$USERDATA"
  sync
}

# --- hostname ----------------------------------------------------------------------------
chk_hostname() {
  [ "$(hostnamectl --static)" = "$HOSTNAME_WANT" ] \
    && grep -Eq "^127\.0\.1\.1\s+$HOSTNAME_WANT(\s|$)" /etc/hosts \
    && ! grep -q raspberrypi /etc/hosts
}
fix_hostname() {
  hostnamectl set-hostname "$HOSTNAME_WANT"
  backup /etc/hosts
  sed -i '/raspberrypi/d; /^127\.0\.1\.1\s/d' /etc/hosts
  printf '127.0.1.1\t%s\n' "$HOSTNAME_WANT" >> /etc/hosts
  sync
}

# --- mDNS on Wi-Fi only (epitaph.local is the Wi-Fi address) -----------------------------
chk_avahi() {
  grep -Eq '^\s*allow-interfaces=wlan0\s*$' "$AVAHI" \
    && systemctl is-enabled --quiet avahi-daemon && systemctl is-active --quiet avahi-daemon
}
fix_avahi() {
  backup "$AVAHI"
  if grep -Eq '^\s*#?\s*allow-interfaces=' "$AVAHI"; then
    sed -Ei 's/^\s*#?\s*allow-interfaces=.*/allow-interfaces=wlan0/' "$AVAHI"
  else
    sed -i '/^\[server\]/a allow-interfaces=wlan0' "$AVAHI"
  fi
  systemctl enable --now avahi-daemon >/dev/null 2>&1
  systemctl restart avahi-daemon
}

# --- kernel command line: memory cgroup, no console blanking, regdom ---------------------
chk_cmdline() {
  local t line; line="$(head -1 "$CMDLINE")"
  [ "$(wc -l < "$CMDLINE")" -le 1 ] || return 1
  for t in $CMDLINE_TOKENS; do grep -qw -- "$t" <<<"$line" || return 1; done
}
fix_cmdline() {
  local t line; line="$(head -1 "$CMDLINE" | sed 's/[[:space:]]*$//')"
  cp -a "$CMDLINE" "$CMDLINE.bak-bootstrap-$STAMP"
  for t in $CMDLINE_TOKENS; do grep -qw -- "$t" <<<"$line" || line="$line $t"; done
  printf '%s\n' "$line" > "$CMDLINE.new" && mv "$CMDLINE.new" "$CMDLINE"; sync
}
# After a reboot the running kernel must agree (the firmware injects cgroup_disable=memory).
chk_memcg_live() { grep -qw memory /sys/fs/cgroup/cgroup.controllers; }

# --- console boot, watchdog, journal, time -----------------------------------------------
chk_target() { [ "$(systemctl get-default)" = multi-user.target ]; }
fix_target() { systemctl set-default multi-user.target >/dev/null 2>&1; }

# The OS already enables the hardware watchdog (40-rpi-enable-watchdog.conf, 1 min): verify,
# and only add a drop-in if that ever disappears.
chk_watchdog() { [ "$(systemctl show -p RuntimeWatchdogUSec --value)" != "0" ]; }
fix_watchdog() {
  mkdir -p /etc/systemd/system.conf.d
  printf '[Manager]\nRuntimeWatchdogSec=1m\n' > /etc/systemd/system.conf.d/90-epitaph-watchdog.conf
  systemctl daemon-reexec
}

chk_journald() {
  same_file "$JOURNALD" 644 "$JOURNALD_BODY" && [ -d /var/log/journal ] \
    && [ "$(systemd-analyze cat-config systemd/journald.conf | grep -E '^Storage=' | tail -1)" = "Storage=persistent" ]
}
fix_journald() {
  mkdir -p "$(dirname "$JOURNALD")" /var/log/journal
  put_file "$JOURNALD" 0644 "$JOURNALD_BODY"
  systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
  systemctl restart systemd-journald
}

chk_tz() { [ "$(timedatectl show -p Timezone --value)" = "$TZ_WANT" ]; }
fix_tz() { timedatectl set-timezone "$TZ_WANT"; }
chk_ntp() { [ "$(timedatectl show -p NTP --value)" = yes ]; }
fix_ntp() { timedatectl set-ntp true; }

# --- sshd: keys only except over the cable -----------------------------------------------
# sshd -T output is captured first: `sshd -T | grep -q` under pipefail fails when grep exits
# early and sshd gets SIGPIPE.
chk_sshd() {
  local wifi cable
  wifi="$(sshd -T 2>/dev/null)"; cable="$(sshd -T -C addr=10.42.0.1,user=pi,host=laptop 2>/dev/null)"
  same_file "$SSHD" 644 "$SSHD_BODY" && [ ! -e /etc/ssh/sshd_config.d/50-cloud-init.conf ] \
    && grep -qx 'passwordauthentication no' <<<"$wifi" \
    && grep -qx 'passwordauthentication yes' <<<"$cable"
}
fix_sshd() {
  [ -e "$SSHD" ] && cp -a "$SSHD" "/root/10-epitaph.conf.bak-bootstrap-$STAMP"
  if [ -e /etc/ssh/sshd_config.d/50-cloud-init.conf ]; then
    mv /etc/ssh/sshd_config.d/50-cloud-init.conf "/root/50-cloud-init.conf.bak-bootstrap-$STAMP"
  fi
  put_file "$SSHD" 0644 "$SSHD_BODY"
  if ! sshd -t; then
    echo "sshd config invalid; restoring" >&2
    [ -e "/root/10-epitaph.conf.bak-bootstrap-$STAMP" ] && cp -a "/root/10-epitaph.conf.bak-bootstrap-$STAMP" "$SSHD"
    return 1
  fi
  systemctl reload ssh
}

# --- epitaph state and packages ----------------------------------------------------------
chk_dirs() {
  local d
  for d in $STATE_DIRS; do [ "$(stat -c %U:%G:%a "$d" 2>/dev/null)" = "$USER_NAME:$USER_NAME:755" ] || return 1; done
}
fix_dirs() { local d; for d in $STATE_DIRS; do install -d -o "$USER_NAME" -g "$USER_NAME" -m 0755 "$d"; done; }

chk_pkgs() { local p; for p in $PACKAGES; do dpkg -s "$p" >/dev/null 2>&1 || return 1; done; }
fix_pkgs() {
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $PACKAGES >/dev/null
}

# --- things only a person can do ---------------------------------------------------------
check_manual() {
  if [ -z "$(wifi_conns)" ]; then
    manual wifi "Wi-Fi is not set up. Yannick, in your own terminal (not Claude Code):  ssh -t pi-eth sudo nmcli --ask device wifi connect \"<SSID>\"   then run this script again."
  else
    say wifi "ok (a system connection exists)"
  fi
  case "$(passwd -S "$USER_NAME" | awk '{print $2}')" in
    P) say password ok ;;
    *) manual password "The $USER_NAME password is locked or unset (keys work; a password is only for the cable). Yannick, in your own terminal:  ssh -t pi sudo passwd $USER_NAME" ;;
  esac
}

# --- effective state, for proving a run changed nothing ----------------------------------
state() {
  local f c
  echo "hostname: $(hostnamectl --static)"
  echo "default-target: $(systemctl get-default)"
  echo "cmdline.txt: $(cat "$CMDLINE")"
  echo "proc-cmdline-flags: $(tr ' ' '\n' </proc/cmdline | grep -E 'cgroup|consoleblank|regdom' | sort | tr '\n' ' ')"
  echo "cgroup-controllers: $(cat /sys/fs/cgroup/cgroup.controllers)"
  echo "wifi-country: $(raspi-config nonint get_wifi_country 2>/dev/null)"
  echo "rfkill-wifi-soft-blocked: $(rfkill list wifi | grep -c 'Soft blocked: yes')"
  while IFS= read -r c; do [ -n "$c" ] && echo "nm[$c]: $(nmcli -g 802-11-wireless.powersave,connection.autoconnect-priority,ipv4.never-default,ipv6.never-default connection show "$c" | tr '\n' ' ')"; done < <(wifi_conns; eth_conns)
  echo "default-route: $(ip route show default | awk '{print $5}' | sort -u | tr '\n' ' ')"
  echo "cloud-init-disabled: $([ -e /etc/cloud/cloud-init.disabled ] && echo yes || echo no)"
  echo "userdata-has-credential: $(chk_userdata && echo no || echo yes)"
  echo "watchdog: $(systemctl show -p RuntimeWatchdogUSec --value)"
  echo "journald-storage: $(systemd-analyze cat-config systemd/journald.conf | grep -E '^(Storage|SystemMaxUse)=' | tr '\n' ' ')"
  echo "timezone: $(timedatectl show -p Timezone --value) ntp: $(timedatectl show -p NTP --value)"
  echo "sshd-wifi: $(sshd -T 2>/dev/null | grep -E '^(passwordauthentication|permitrootlogin|kbdinteractiveauthentication) ' | tr '\n' ' ')"
  echo "sshd-cable: $(sshd -T -C addr=10.42.0.1,user=pi,host=laptop 2>/dev/null | grep -E '^passwordauthentication ')"
  echo "avahi: $(grep -E '^\s*allow-interfaces=' "$AVAHI") $(systemctl is-active avahi-daemon)"
  echo "password-status: $(passwd -S "$USER_NAME" | awk '{print $2}')"
  for f in "$SUDOERS" "$JOURNALD" "$SSHD" "$AVAHI" "$CMDLINE" /etc/hosts /etc/hostname "$USERDATA"; do
    [ -e "$f" ] && echo "file $f: $(stat -c '%a %U:%G %Y' "$f") $(sha256sum < "$f" | cut -c1-16)"
  done
  ls /etc/ssh/sshd_config.d/ | sed 's/^/sshd.d: /'
  for d in $STATE_DIRS; do echo "dir $d: $(stat -c '%U:%G %a' "$d" 2>/dev/null || echo missing)"; done
  for c in $PACKAGES; do echo "pkg $c: $(dpkg-query -W -f '${Version}' "$c" 2>/dev/null || echo missing)"; done
}

if [ "$MODE" = state ]; then state; exit 0; fi

echo "== pi_bootstrap ($MODE) on $(hostname) at $(date -Is)"
item sudoers        chk_sudoers       fix_sudoers       0
item wifi-country   chk_country       fix_country       0
check_manual
item wifi-settings  chk_wifi_settings fix_wifi_settings 0
item cable-no-route chk_eth           fix_eth           0
item cloud-init-off chk_cloudinit     fix_cloudinit     1
item user-data-cred chk_userdata      fix_userdata      0
item hostname       chk_hostname      fix_hostname      0
item avahi-wlan0    chk_avahi         fix_avahi         0
item cmdline        chk_cmdline       fix_cmdline       1
item console-boot   chk_target        fix_target        1
item watchdog       chk_watchdog      fix_watchdog      0
item journald       chk_journald      fix_journald      0
item timezone       chk_tz            fix_tz            0
item ntp            chk_ntp           fix_ntp           0
item sshd           chk_sshd          fix_sshd          0
item state-dirs     chk_dirs          fix_dirs          0
item packages       chk_pkgs          fix_pkgs          0
if [ "$NEED_REBOOT" = 0 ] && chk_cmdline; then
  if chk_memcg_live; then say memory-cgroup ok; else say memory-cgroup "MISSING (reboot pending, or the firmware override failed: death_mode = deadline)"; FAILED+=(memory-cgroup); fi
fi

echo "== changed: ${#CHANGED[@]} (${CHANGED[*]:-none}); drift: ${#DRIFT[@]} (${DRIFT[*]:-none}); failed: ${#FAILED[@]} (${FAILED[*]:-none})"
for m in "${MANUAL[@]}"; do echo "!! $m"; done
[ "${#FAILED[@]}" -eq 0 ] || exit 1
[ "$MODE" = check ] && [ "${#DRIFT[@]}" -gt 0 ] && exit 1
if [ "$NEED_REBOOT" = 1 ]; then echo "== reboot needed"; exit 10; fi
exit 0
