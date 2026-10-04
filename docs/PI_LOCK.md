# The Pi lock and the laptop lock

There is one Pi and one laptop CPU budget, shared by every job (BUILD_PLAN 0.5, 0.6, 8.3).
All jobs start on the laptop, so both locks are plain `flock` files on the laptop
(`tools/lock.sh`, wrapped by `tools/pi_lock.sh` and `tools/laptop_lock.sh`).

## Use

```
tools/pi_lock.sh run <name> <minutes> -- <command...>      # wait, hold, run, release
tools/pi_lock.sh status                                    # free, or who holds it and since when
tools/laptop_lock.sh run <name> <minutes> -- <command...>  # the same for llama-server on the laptop
```

- **Every command that touches the Pi** (SSH commands, rsync, deploys, spikes, benches,
  reboots, `pi_bootstrap.sh`) runs inside `pi_lock.sh run`. Read-only probes too: a probe that
  lands in the middle of someone's benchmark distorts it.
- **Hold it per job, not per session.** One spike, one bench, one bootstrap run. Release
  between jobs so the queue moves (8.5 gives the order when several jobs wait).
- **`<minutes>` is a hard limit.** The command is stopped (`timeout`, then SIGKILL 30 s later)
  when it overruns, and the lock is released. Ask for what the job needs plus a margin.
- **Waiting** is up to 4 hours by default (`EPITAPH_LOCK_WAIT_S`). The waiter prints who
  holds the lock. Exit code 75 means it gave up waiting.
- **Stale locks cannot happen:** the kernel drops a `flock` when its holder dies. The owner
  file (`~/.local/state/epitaph/locks/pi.owner`) is only a label for `status`.

## Long jobs

The lock holder is a process on the laptop, but long Pi work must not depend on an SSH
session (Wi-Fi SSH drops; PI_FACTS "Lessons"). Start it as a detached unit on the Pi and poll,
all inside one lock hold:

```
tools/pi_lock.sh run job 60 -- bash -c '
  ssh pi "sudo -n systemd-run --unit=epitaph-job --uid=pi --gid=pi --setenv=HOME=/home/pi <cmd>" &&
  while ssh pi "systemctl is-active --quiet epitaph-job"; do sleep 15; done'
```

If the laptop-side poll is killed at the time limit, the unit keeps running on the Pi and
the lock is free again: whoever takes the lock next must check `systemctl list-units
'epitaph-*' 'llama-*'` first and stop or wait for leftovers.

## What the lock does not cover

- It serialises jobs started from this laptop only. Someone working on the Pi by hand is not
  serialised; check `who` and the load average before a timing-sensitive run.
- The 25-hour soak and installed services run on the Pi without holding the lock; while
  they run, the lock is taken only for short read-only checks.
- Always log `vcgencmd get_throttled` before and after a load test (power, PI_FACTS).
