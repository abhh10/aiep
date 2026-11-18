#!/bin/bash
# usage: run_eval.sh <run_id> <predictions: "gold" | absolute path to .jsonl>
# Runs one SWE-bench evaluation of pytest-dev__pytest-5227 via aiep.stock_eval
# (reduced-privilege wrapper around the stock harness). All artifacts -> runs/m1/<run_id>/
# Disk policy: must have >= MIN_START GiB free to start; hard stop (kill container) if free < 12 GiB.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_ID=$1; PRED=$2
INSTANCE=pytest-dev__pytest-5227
IMAGE=swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-5227:latest
MIN_START=16
D=$REPO/runs/m1/$RUN_ID
mkdir -p "$D" && cd "$D" || exit 2

free()  { df -k "$REPO" | awk 'NR==2{printf "%.3f", $4/1048576}'; }
rawgib(){ ls -ls "$HOME/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw" | awk '{printf "%.3f", $1*512/1073741824}'; }
below12(){ awk -v f="$1" 'BEGIN{exit !(f<12.0)}'; }
startlow(){ awk -v f="$1" -v m="$MIN_START" 'BEGIN{exit !(f<m)}'; }

B=$(free); echo "$B" > disk_before_gib.txt
if startlow "$B"; then echo "NOT STARTED: free ${B} GiB < ${MIN_START} GiB required to start" | tee STOPPED.txt; exit 90; fi

cp "$0" run_eval.sh
CMD="PYTHONPATH=$REPO/src HF_HOME=$REPO/.cache/hf python -m aiep.stock_eval -i $INSTANCE -p $PRED --run-id $RUN_ID -t 900 --report-dir reports"
{
  echo "run_id: $RUN_ID"; echo "predictions: $PRED"; echo "instance: $INSTANCE"
  echo "started_utc: $(date -u +%FT%TZ)"
  echo "swebench: $("$REPO/.venv/bin/python" -c 'import importlib.metadata as m;print(m.version("swebench"))')"
  echo "image_digest: $(docker image inspect "$IMAGE" --format '{{index .RepoDigests 0}}')"
  echo "docker: $(docker version --format 'client {{.Client.Version}} / server {{.Server.Version}}')"
  echo "docker_mem_bytes: $(docker info --format '{{.MemTotal}}')"
  echo "disk_before_gib: $B"; echo "min_start_gib: $MIN_START; hard_stop_gib: 12"
  echo "command: $CMD"
} > meta.txt

# disk sampler: every 3 s log free GiB + Docker.raw allocation; hard stop below 12 GiB
(
  while true; do
    F=$(free); echo "$(date -u +%FT%TZ) free_gib=$F docker_raw_gib=$(rawgib)" >> disk_samples.log
    if below12 "$F"; then
      echo "HARD STOP (sampler): free ${F} GiB < 12" >> STOPPED.txt
      for c in $(docker ps -q --filter "name=sweb.eval" --filter "name=$RUN_ID"); do docker kill "$c" >/dev/null 2>&1; done
      break
    fi
    sleep 3
  done
) &
SAMPLER=$!

# watcher: snapshot container privileges once, sample container memory/cpu
(
  for i in $(seq 1 1500); do
    c=$(docker ps -a --filter "name=sweb.eval" --filter "name=$RUN_ID" -q | head -1)
    if [ -n "$c" ]; then
      [ -s container_inspect.json ] || docker inspect "$c" > container_inspect.json 2>&1
      docker stats --no-stream --format '{{.Name}} mem={{.MemUsage}} cpu={{.CPUPerc}} pids={{.PIDs}}' "$c" >> container_stats.log 2>&1
    fi
    sleep 4
  done
) &
WATCH=$!

PYTHONPATH=$REPO/src HF_HOME=$REPO/.cache/hf \
  "$REPO/.venv/bin/python" -m aiep.stock_eval -i "$INSTANCE" -p "$PRED" \
  --run-id "$RUN_ID" -t 900 --report-dir reports > stdout.log 2> stderr.log
echo $? > exit_code.txt
kill $WATCH $SAMPLER 2>/dev/null; wait $WATCH $SAMPLER 2>/dev/null
echo "$(date -u +%FT%TZ) free_gib=$(free) docker_raw_gib=$(rawgib) (final)" >> disk_samples.log

A=$(free); echo "$A" > disk_after_gib.txt
{
  echo "ended_utc: $(date -u +%FT%TZ)"; echo "exit_code: $(cat exit_code.txt)"; echo "disk_after_gib: $A"
  echo "min_free_gib_during_run: $(awk -F'free_gib=' 'NF>1{split($2,a," "); print a[1]}' disk_samples.log | sort -n | head -1)"
  echo "leftover_eval_containers: $(docker ps -a --filter name=sweb.eval --filter name=$RUN_ID -q | wc -l | tr -d ' ')"
  [ -f STOPPED.txt ] && echo "STOPPED: $(cat STOPPED.txt | tr '\n' ' ')"
} >> meta.txt
cat meta.txt
