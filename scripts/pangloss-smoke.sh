#!/bin/bash -l
# Submit with sbatch using --chdir and --output paths inside the SSD run folder.
set -euo pipefail

RUN_DIR=$(realpath "${1:?Usage: pangloss-smoke.sh /ssd_workspace/gbrach/RUN}")
case "$RUN_DIR" in
    /ssd_workspace/gbrach/*) ;;
    *) printf 'Pangloss GPU jobs must use /ssd_workspace/gbrach\n' >&2; exit 2 ;;
esac
cd "$RUN_DIR"
mkdir -p tmp details results
export TMPDIR="$RUN_DIR/tmp"
export XDG_CACHE_HOME="$RUN_DIR/tmp/cache"
export CUDA_CACHE_PATH="$RUN_DIR/tmp/cuda"
export PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-4}"

log() { printf '%s  %s\n' "$(date '+%F %T')" "$*" | tee -a "$RUN_DIR/run.log"; }
stage=preflight
detail_log=details/slurm.log
failed() {
    code=$?
    log "FAILED during $stage. Details: $RUN_DIR/$detail_log"
    printf 'failed\n' > "$RUN_DIR/status.txt"
    exit "$code"
}
trap failed ERR
monitor_pids=()
stop_monitors() {
    local pid
    for pid in "${monitor_pids[@]}"; do
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
    monitor_pids=()
}
trap stop_monitors EXIT

module load dorado/2.1.0
test -n "${SLURM_JOB_ID:-}"
test -n "${CUDA_VISIBLE_DEVICES:-}"
expected_gpus=${HAPLOCALLER_EXPECTED_GPUS:-1}
batch_size=${HAPLOCALLER_BATCHSIZE:-0}
[[ "$expected_gpus" =~ ^[1-9][0-9]*$ ]]
[[ "$batch_size" =~ ^[0-9]+$ ]]
/usr/bin/python3 - "$expected_gpus" "$batch_size" > details/allocation.json <<'PY'
import json
import os
import sys

visible = os.environ["CUDA_VISIBLE_DEVICES"].split(",")
expected = int(sys.argv[1])
if len(visible) != expected or any(not value or value == "-1" for value in visible):
    raise SystemExit(f"Expected {expected} GPUs; CUDA_VISIBLE_DEVICES={visible!r}")
json.dump({"job_id": os.environ["SLURM_JOB_ID"],
           "cuda_visible_devices": visible, "expected_gpus": expected,
           "device_argument": "cuda:all", "batch_size": int(sys.argv[2])}, sys.stdout, indent=2)
print()
PY
batch_args=()
if (( batch_size > 0 )); then
    batch_args=(--batchsize "$batch_size")
fi
model=$(cat "$RUN_DIR/model.txt")
test -d "$model"
case "$(realpath "$model")" in
    "$RUN_DIR"/models/*) ;;
    *) log 'Model must be staged in this SSD run folder.'; exit 2 ;;
esac
if [ -e calls ]; then
    log 'Existing calls directory found; use a new run directory to avoid overwriting results.'
    exit 2
fi

printf 'running\n' > status.txt
log "$expected_gpus GPU(s) allocated; Slurm job $SLURM_JOB_ID on $(hostname); visible devices: $CUDA_VISIBLE_DEVICES."
dorado --version > details/dorado-version.txt 2>&1
nvidia-smi > details/gpu.txt
printf 'CUDA_VISIBLE_DEVICES=%s\n' "$CUDA_VISIBLE_DEVICES" >> details/gpu.txt
log "Basecalling 300 test reads with $(basename "$model"); Q10; unclassified reads retained."
log "One Dorado process using cuda:all; batch size $batch_size (0 = automatic)."
nvidia-smi --query-gpu=timestamp,index,uuid,utilization.gpu,memory.used \
    --format=csv,nounits -l 1 > details/gpu-usage.csv 2> details/gpu-monitor.log &
monitor_pids+=("$!")
nvidia-smi --query-compute-apps=timestamp,pid,gpu_uuid,used_gpu_memory,process_name \
    --format=csv,nounits -l 1 > details/gpu-processes.csv 2> details/process-monitor.log &
monitor_pids+=("$!")
SECONDS=0
stage=basecalling
detail_log=details/basecaller.log
dorado basecaller "$model" "$RUN_DIR/input/pod5" \
    --device cuda:all --recursive "${batch_args[@]}" \
    --kit-name SQK-NBD114-96 --sample-sheet "$RUN_DIR/input/sample_sheet.csv" \
    --min-qscore 10 --output-dir "$RUN_DIR/calls" --emit-summary \
    > details/basecaller.stdout.log 2> details/basecaller.log &
dorado_pid=$!
printf '%s\n' "$dorado_pid" > details/dorado-pid.txt
wait "$dorado_pid"
printf '%s\n' "$SECONDS" > details/basecalling-seconds.txt
stop_monitors
log "Basecalling complete in ${SECONDS}s. Checking BAMs and exporting FASTQ.gz and raw signals."
stage=export
detail_log=details/export.log
/usr/bin/python3 "$RUN_DIR/scripts/export_smoke.py" "$RUN_DIR" \
    > details/export.log 2>&1
cat results/summary.txt | tee -a run.log
printf 'complete\n' > status.txt
log "Finished. Results: $RUN_DIR/results"
