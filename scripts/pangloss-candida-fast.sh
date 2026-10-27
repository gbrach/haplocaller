#!/bin/bash -l
# One Dorado process on two GPUs; native demultiplexed FASTQ output, no POD5 export.
set -euo pipefail
RUN_DIR=$(realpath "${1:?Supply the prepared SSD run directory}")
RUN_MODE=${2:---pilot-only}
case "$RUN_MODE" in --pilot-only|--full) ;; *) printf 'Use --pilot-only or --full\n' >&2; exit 2 ;; esac
case "$RUN_DIR" in /ssd_workspace/gbrach/*) ;; *) exit 2 ;; esac
cd "$RUN_DIR"
export TMPDIR="$RUN_DIR/tmp"
export XDG_CACHE_HOME="$RUN_DIR/tmp/cache"
export CUDA_CACHE_PATH="$RUN_DIR/tmp/cuda"
export PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-16}"
export TZ=Europe/Paris
mkdir -p "$TMPDIR" "$XDG_CACHE_HOME" "$CUDA_CACHE_PATH"
log() { printf '%s  %s\n' "$(date '+%F %T %Z')" "$*" | tee -a run.log; }
stage=preflight
detail=logs/slurm.log
monitor_pid=''
progress_pid=''
dorado_pid=''
cleanup() {
    for pid in "$monitor_pid" "$progress_pid" "$dorado_pid"; do
        if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
    done
}
failed() {
    local code=$?
    trap - ERR
    printf 'failed\n' > status.txt
    log "FAILED during $stage (exit $code). See $RUN_DIR/$detail"
    exit "$code"
}
interrupted() {
    printf 'interrupted\n' > status.txt
    log "Interrupted during $stage; outputs may be incomplete."
    exit 143
}
trap failed ERR
trap cleanup EXIT
trap interrupted TERM INT
test -n "${SLURM_JOB_ID:-}"
test ! -e calls
test ! -e pilot
test ! -e fastq
test ! -e tmp/pilot-native
test ! -e tmp/native-calls
module load dorado/2.1.0
dorado --version > logs/dorado-version.txt 2>&1
model=$(cat config/model.txt)
case "$(realpath "$model")" in "$RUN_DIR"/models/*) ;; *) exit 2 ;; esac
test -f "$model/config.toml"
/usr/bin/python3 - "$RUN_DIR" <<'PY'
import json,os,sys
from pathlib import Path
root=Path(sys.argv[1])
devices=os.environ.get('CUDA_VISIBLE_DEVICES','').split(',')
assert len(devices)==2 and len(set(devices))==2 and all(d and d!='-1' for d in devices), devices
manifest=json.loads((root/'config/input-manifest.json').read_text())
for entry in manifest:
    stat=Path(entry['path']).stat()
    assert stat.st_size==entry['bytes'] and stat.st_mtime_ns==entry['mtime_ns'], entry['path']
settings=json.loads((root/'config/settings.json').read_text())
assert set(Path(settings['input']).rglob('*.pod5'))=={Path(e['path']) for e in manifest}
(root/'logs/allocation.json').write_text(json.dumps({'job_id':os.environ['SLURM_JOB_ID'],
 'cuda_visible_devices':devices,'device_argument':'cuda:all'},indent=2)+'\n')
PY
input=/ssd_workspace/gbrach/candida_pod5/Candida_plate3
first_input=$(/usr/bin/python3 -c 'import json;print(json.load(open("config/input-manifest.json"))[0]["path"])')
input_reads=$(/usr/bin/python3 -c 'import json;print(json.load(open("config/settings.json"))["input_reads"])')
common=(--device cuda:all --kit-name SQK-NBD114-96 --sample-sheet "$RUN_DIR/config/sample_sheet.csv"
        --min-qscore 0 --emit-fastq --emit-summary)
printf 'pilot\n' > status.txt
log "Job $SLURM_JOB_ID on $(hostname): two GPUs, visible devices $CUDA_VISIBLE_DEVICES."
log "Input: $input ($input_reads reads); model: $(basename "$model")."
log "FAST basecalling and sample demultiplexing; Q0; unclassified retained; FASTQ.gz output."
nvidia-smi > logs/gpus.txt
nvidia-smi --query-gpu=timestamp,index,uuid,utilization.gpu,memory.used --format=csv,nounits -l 30 \
    > logs/gpu-usage.csv 2> logs/gpu-monitor.log &
monitor_pid=$!
stage=pilot
detail=logs/pilot-basecaller.log
log "Checking the first 1,000 reads; requested mode: $RUN_MODE."
dorado basecaller "$model" "$first_input" "${common[@]}" --batchsize 64 --max-reads 1000 \
    --output-dir "$RUN_DIR/tmp/pilot-native" > logs/pilot.stdout.log 2> "$detail" &
dorado_pid=$!
wait "$dorado_pid"
dorado_pid=''
stage=pilot_compression
detail=logs/pilot-compression.log
/usr/bin/python3 scripts/flatten_native_fastq.py "$RUN_DIR/tmp/pilot-native" "$RUN_DIR/pilot/fastq" \
    > "$detail" 2>&1
stage=pilot_verification
detail=logs/pilot-verification.log
/usr/bin/python3 scripts/verify_native_fastq.py "$RUN_DIR" "$RUN_DIR/pilot" logs/pilot-basecaller.log \
    --minimum-reads 1000 --workers 8 --flat > "$detail" 2>&1
if [[ "$RUN_MODE" == --pilot-only ]]; then
    printf 'pilot_complete\n' > status.txt
    log "Pilot complete. FASTQs: $RUN_DIR/pilot/fastq. Full dataset not started."
    exit 0
fi
log "Pilot passed. Starting all $input_reads input reads with automatic batch sizing."
stage=basecalling
detail=logs/basecaller.log
printf 'basecalling\n' > status.txt
SECONDS=0
dorado basecaller "$model" "$input" "${common[@]}" --recursive --batchsize 0 \
    --output-dir "$RUN_DIR/tmp/native-calls" > logs/basecaller.stdout.log 2> "$detail" &
dorado_pid=$!
printf '%s\n' "$dorado_pid" > logs/dorado-pid.txt
(
    while sleep 300; do
        kill -0 "$dorado_pid" 2>/dev/null || exit 0
        size=$(du -sh tmp/native-calls 2>/dev/null | cut -f1)
        log "Basecalling in progress; output on disk: ${size:-0}."
    done
) &
progress_pid=$!
wait "$dorado_pid"
dorado_pid=''
kill "$progress_pid" 2>/dev/null || true
wait "$progress_pid" 2>/dev/null || true
progress_pid=''
kill "$monitor_pid" 2>/dev/null || true
wait "$monitor_pid" 2>/dev/null || true
monitor_pid=''
printf '%s\n' "$SECONDS" > logs/basecalling-seconds.txt
log "Basecalling finished in ${SECONDS}s. Compressing FASTQs."
stage=compression
detail=logs/compression.log
printf 'compressing\n' > status.txt
/usr/bin/python3 scripts/flatten_native_fastq.py "$RUN_DIR/tmp/native-calls" "$RUN_DIR/fastq" \
    > "$detail" 2>&1
log 'Compression finished. Checking FASTQs and per-sample counts.'
stage=verification
detail=logs/verification.log
printf 'verifying\n' > status.txt
/usr/bin/python3 scripts/verify_native_fastq.py "$RUN_DIR" "$RUN_DIR" logs/basecaller.log \
    --minimum-reads "$input_reads" --workers 8 --flat > "$detail" 2>&1
cat "$detail" | tee -a run.log
printf 'complete\n' > status.txt
log "Complete. FASTQs: $RUN_DIR/fastq; counts: $RUN_DIR/sample_counts.tsv"
