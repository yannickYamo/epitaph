# Phase 0c, round 2: part C (Pi ops)

Branch `ws/c-ops`. Card: the owner's review 2, items F11 (swap) and F12 (name resolution).
Every Pi step ran under the Pi lock, each hold only a few minutes long. The one reboot
(about 90 s) came before A's current bench started, and nothing else was running on the Pi
at the time. `vcgencmd get_throttled` was `0x0` throughout.

## Headline for the integrator

1. **F12's cause was the Pi's location, not mDNS.** `wlan0` had not connected at all since the
   07:57 boot: the Pi's saved Wi-Fi network is not among the 100+ networks it can see where it
   stands now. Avahi was healthy (no conflicts or withdrawals in any boot's journal, and
   `publish-workstation` was already on), so I left its settings alone (QUESTIONS C #6).
   Until someone adds a network the Pi can reach (an owner action, because it needs the
   secret), `pi` will not answer and every tool uses the cable.
2. **Because of that, the Pi had no route out: no NTP, and a clock 88 minutes slow.** The
   cable's IPv4 route is now allowed at metric 800, behind Wi-Fi's 600. Wi-Fi still wins
   whenever it is up, which keeps V6 intact, and with Wi-Fi out of range the laptop's shared
   connection provides NTP. After the reboot the clock was stepped forward 88 min and
   synchronised. This relaxes 8.6 step 5 (`never-default`), so it is logged as QUESTIONS C #4
   with the one-line revert. Pi-side timestamps from that morning are 88 min slow
   (QUESTIONS C #8, for A).
3. **Swap (F11) is done and verified after a reboot.** zram is the only swap device (2 GB,
   zstd, `backing_dev` none), `/var/swap` is gone (2 GB back on the card, and a smaller SD
   backup), and swappiness is 10. The creature still gets `memory.swap.max = 0`: a unit test
   covers every reset, and S3b on the real Pi read `0` and got an OOM death with zram present.
4. **Every tool that talks to the Pi falls back to `pi-eth`**: `tools/pi_host.sh` (new),
   `pi_bootstrap.sh`, `s3_run.sh`, `spike/pi_run.sh`, `build_llamacpp.sh --pi`,
   `sd_backup.sh` (cable first), `download_models.py push` (cable first) and
   `epitaph display --connect pi` (which now means `pi,pi-eth`).

## Built

- **`tools/pi_host.sh`**: prints the first SSH alias that answers, trying `pi` and then
  `pi-eth`. It can be sourced (`pi_host`) or run directly. `PI_HOST` pins one alias and
  `PI_HOSTS` changes the order. The probe is bounded by `timeout`, because a failing mDNS
  lookup can hang for longer than ssh's `ConnectTimeout`.
- **`pi_bootstrap.sh`**:
  - Laptop side: picks the host automatically (`== host: …`) and picks again while it waits
    for a reboot.
  - New items `swap-zram-only` (`/etc/rpi/swap.conf.d/90-epitaph.conf`, `Mechanism=zram`;
    needs a reboot), `swappiness` (`/etc/sysctl.d/90-epitaph.conf`) and `cable-fallback`
    (replaces `cable-no-route`).
  - Post-reboot check `swap-live`: zram is the only swap and has no backing device, and no
    card file is left.
  - Reports `clock-synced`, because an enabled NTP service is not the same as a working one.
  - Reports a saved Wi-Fi connection that is not connected, and prints the command for the
    owner.
  - `--state` now also prints the swap state, the swappiness and the cable route.
- **`build_llamacpp.sh --pi`** takes the Pi lock before it touches the Pi. It used to `scp`
  outside the lock.
- **`display/remote.py`**:
  - `parse_hosts`, and `Tunnel(..., fallbacks=)`: each restart tries the hosts in order, so
    the view moves to the cable when mDNS fails and back to Wi-Fi when it returns.
  - When every host fails, the error lists each host's reason.
  - ssh now runs with `ConnectTimeout=8`.
- **`backend/models.py`**: `pick_host()` (cable first) and `--host` defaulting to it.
- **Docs**:
  - PI_CHANGES has four rows.
  - PI_FACTS has a Swap row and a new section, "Reaching the Pi". It covers the order of
    aliases, an optional ssh `Match … exec` block for a plain `ssh pi` (tested with
    `ssh -G`) and the owner's reservation options: the router for Wi-Fi, and `dhcp-host` in
    the laptop's `dnsmasq-shared.d` for the cable.
  - BUILD_PLAN 8.6 step 5, its risk row and the step-0 table now describe the cable as a route
    behind Wi-Fi, and gate S0.5 is marked for a re-check.

## Tested

- `make check`: 664 passed (ruff, pyright, pytest, sim, estimate).
- New unit tests: `test_parse_hosts`,
  `test_tunnel_falls_back_to_the_next_host_and_prefers_the_first_again`,
  `test_tunnel_reports_every_host_when_all_fail`, `test_every_life_starts_with_swap_off`
  and `test_pick_host_prefers_the_cable_and_falls_back`.
- On the Pi:
  - `pi_bootstrap.sh --apply`: 3 changed, then a reboot, then `--check` with 0 drift and
    `swap-live` ok.
  - A second `--apply`: 0 changed, and its `--state` was identical to the first.
  - `pytest -m pi tests/pi`: 2 passed. That covers the bootstrap `--check` through the
    fallback (it picked `pi-eth`) and S3b. The S3b result is in
    `tools/spike/s3_results/s3b-20260930-104755.json`.
- After the reboot:
  - `/proc/swaps` lists `/dev/zram0` only, `/var/swap` does not exist, and
    `/proc/sys/vm/swappiness` is 10.
  - The default route is via `eth0` at metric 800, with ping and DNS working.
  - `NTPSynchronized=yes` (offset 3 ms).

## Left

- **Owner**:
  - Add a Wi-Fi network the Pi can reach where it now stands (`ssh -t pi-eth sudo nmcli
    --ask device wifi connect "<SSID>"`, in your own terminal), or keep it on the cable.
  - Gate S0.5 needs a re-check once the Pi is back on Wi-Fi (Wi-Fi first at 600).
  - F10: set a new Pi password (`ssh -t pi-eth sudo passwd pi`).
  - Optional: address reservations (PI_FACTS "Reaching the Pi").
- **C, next round** (I could not get the Pi lock again: A's ladders held it from 10:48 on):
  - Run `pi_bootstrap.sh --check` once on the Pi to see the new `wifi` "set up but not
    connected" line. Its `nmcli` parsing was tested on the laptop only.
  - The persistent journal lists boot -1 as ending at 22:35 (the journald restart in 0b's
    drift test), yet spikes ran on the Pi for hours after that. Check whether journald
    kept writing to disk after a restart. If it did not, `fix_journald` needs a
    `journalctl --flush`, or a check that `/var/log/journal` is actually being written.
- **A**: `spike/pi_run.sh` still probes the host and syncs its scripts before taking the
  lock, as it did before this round. I changed only its host choice, to avoid conflicting
  with A's round-2 edits.
- **B and L**: QUESTIONS C #7 answers A's question 9. `cpu.max` does bind below 3 cores. If a
  profile wants the S4 gain, it should hold `cpu_share = 3.0` until erosion, within F2.
