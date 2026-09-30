#!/usr/bin/env bash
# Run spike S3, S3b or S3c on the Pi (BUILD_PLAN 8.5). Agent C. Run it under the Pi lock:
#   tools/pi_lock.sh run C 30 -- tools/spike/s3_run.sh s3b
#   tools/pi_lock.sh run C 60 -- tools/spike/s3_run.sh s3  --model /var/lib/epitaph/models/.../Q4_K_M.gguf --eviction
#   tools/pi_lock.sh run C 30 -- tools/spike/s3_run.sh s3c --model ...
# The probe runs as a throwaway Delegate=yes unit (epitaph-spike-<id>) as user pi, through
# the real body code. Results land in tools/spike/s3_results/<id>-<time>.json.
set -euo pipefail

SPIKE="${1:?s3 | s3b | s3c}"; shift
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=tools/pi_host.sh
. "$HERE/tools/pi_host.sh"
HOST="$(pi_host)"  # `pi`, or the cable when mDNS fails (BUILD_PLAN F12)
REMOTE=/tmp/epitaph-spike
UNIT="epitaph-spike-$SPIKE"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$HERE/tools/spike/s3_results/$SPIKE-$STAMP.json"
mkdir -p "$(dirname "$OUT")"
ssh_() { ssh -o BatchMode=yes -o ServerAliveInterval=15 "$HOST" "$@"; }

echo "== $SPIKE on $HOST at $STAMP"
ssh_ "rm -rf $REMOTE && mkdir -p $REMOTE/src $REMOTE/sync"
tar -C "$HERE/src" --exclude=__pycache__ -cf - epitaph | ssh_ "tar -C $REMOTE/src -xf -"
scp -q "$HERE/tools/spike/s3_probe.py" "$HOST:$REMOTE/"
echo "throttled before: $(ssh_ vcgencmd get_throttled) $(ssh_ vcgencmd measure_temp)"

ssh_ "sudo -n systemctl reset-failed $UNIT 2>/dev/null; sudo -n systemd-run --quiet --unit=$UNIT \
  -p Delegate=yes --uid=pi --gid=pi --setenv=HOME=/home/pi --setenv=PYTHONPATH=$REMOTE/src \
  --working-directory=$REMOTE --collect \
  /usr/bin/python3 $REMOTE/s3_probe.py $SPIKE --out $REMOTE/result.json --sync-dir $REMOTE/sync $*"

if [ "$SPIKE" = s3b ]; then
  # The root side of the network test: block the creature cgroup, then remove the rule.
  ssh_ 'bash -s' <<'REMOTE_EOF'
set -euo pipefail
S=/tmp/epitaph-spike/sync
for _ in $(seq 240); do [ -s $S/creature_path ] && break; sleep 0.5; done
cg="$(cat $S/creature_path)"; rel="${cg#/sys/fs/cgroup/}"
level="$(awk -F/ '{print NF}' <<<"$rel")"
sudo -n nft -f - <<NFT
table inet epitaph_spike {
  chain output {
    type filter hook output priority 0; policy accept;
    socket cgroupv2 level $level "$rel" oifname != "lo" counter reject
  }
}
NFT
sudo -n nft list table inet epitaph_spike
touch $S/nft_ready
for _ in $(seq 240); do [ -e $S/probe_done ] && break; sleep 0.5; done
sudo -n nft list table inet epitaph_spike | grep counter || true
sudo -n nft delete table inet epitaph_spike
echo "nft rule removed"
REMOTE_EOF
fi

while ssh_ "systemctl is-active --quiet $UNIT"; do sleep 5; done
ssh_ "journalctl -u $UNIT --since '-3h' --no-pager -o cat | tail -60" || true
echo "throttled after: $(ssh_ vcgencmd get_throttled) $(ssh_ vcgencmd measure_temp)"
scp -q "$HOST:$REMOTE/result.json" "$OUT"
echo "result: $OUT"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('ok' if d.get('ok', True) else 'NOT OK')" "$OUT"
