#!/usr/bin/env bash
# Spike S7: generation speed and CPU temperature against the CPU clock cap (cpufreq) on the Pi 4.
# Runs on the Pi as root (writes scaling_max_freq), restores 1800 MHz at the end.
#   sudo bash s7_clock.sh <models_dir> <model> <quants...>   -> JSON lines on stdout
set -u
MODELS="$1"; MODEL="$2"; shift 2
BIN=/home/pi/llama.cpp/build/bin/llama-bench
restore() { for c in /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq; do echo 1800000 > "$c"; done; }
trap restore EXIT
for q in "$@"; do
  for mhz in 1800 1500 1200 900 600; do
    for c in /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq; do echo $((mhz*1000)) > "$c"; done
    sleep 3
    out=$(taskset -c 1-3 "$BIN" -m "$MODELS/$MODEL/$q.gguf" -t 2 -p 64 -n 32 -r 2 -o json 2>/dev/null)
    tg=$(echo "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(round([x["avg_ts"] for x in d if x["n_gen"]>0][0],3))' 2>/dev/null)
    pp=$(echo "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(round([x["avg_ts"] for x in d if x["n_prompt"]>0][0],3))' 2>/dev/null)
    temp=$(vcgencmd measure_temp | tr -dc '0-9.')
    cur=$(cat /sys/devices/system/cpu/cpu1/cpufreq/scaling_cur_freq)
    thr=$(vcgencmd get_throttled | cut -d= -f2)
    echo "{\"model\":\"$MODEL\",\"quant\":\"$q\",\"cap_mhz\":$mhz,\"cur_mhz\":$((cur/1000)),\"threads\":2,\"tg_tok_s\":${tg:-null},\"pp_tok_s\":${pp:-null},\"temp_c\":$temp,\"throttled\":\"$thr\"}"
  done
done
