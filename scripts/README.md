# Pangloss smoke test

For the guided terminal interface, use the `bin/haplocaller` command and [user guide](../README.md). The scripts below document the earlier diagnostic runs.

This is a real, tested processing prototype for the 300-read sample, not the finished interactive launcher. It uses the existing Dorado 2.1.0 installation, a pinned HAC model, Q10, and synthetic barcode aliases. It retains failing and unclassified reads so the output can be checked completely.

The successful run is `/ssd_workspace/gbrach/haplocaller-smoke-20261019.lIGvW1` on Pangloss (Slurm job `1158242`). Locally, its exported files and logs are in `../runs/pangloss-smoke-20261019.lIGvW1/`.

## Files

- `prepare_smoke.py` validates input IDs/counts and creates the native Dorado sample sheet using POD5 experiment and flowcell metadata.
- `pangloss-smoke.sh` runs the GPU basecaller and exporter under Slurm and writes a readable `run.log`.
- `export_smoke.py` produces per-alias BAM/FASTQ.gz files in separate pass/fail folders and POD5 archives containing all input signals, then independently reopens and validates those outputs.

The Python helpers use `/usr/bin/python3`, where Pangloss has POD5 0.3.6 and pysam 0.22.1. The default Miniforge Python does not have the same packages.

## Repeat the test

Run the following preparation on the Pangloss login node. The `haplocaller_scripts` value must name a copy of this repository's `scripts` directory on Pangloss. All compute-job files must be staged on the SSD, including the scripts and model. A new run folder avoids overwriting earlier results.

```bash
haplocaller_scripts=/path/to/haplocaller/scripts
run_dir=$(mktemp -d /ssd_workspace/gbrach/haplocaller-smoke.XXXXXX)
mkdir -p "$run_dir/input" "$run_dir/models" "$run_dir/tmp" "$run_dir/details" "$run_dir/scripts"
cp -a /shared/home/gbrach/dorado-check.0Bcg9w/testdata/. "$run_dir/input/"
cp "$haplocaller_scripts/prepare_smoke.py" "$haplocaller_scripts/export_smoke.py" \
   "$haplocaller_scripts/pangloss-smoke.sh" "$run_dir/scripts/"

cd "$run_dir"
export TMPDIR="$run_dir/tmp"
export XDG_CACHE_HOME="$run_dir/tmp/cache"
export PYTHONDONTWRITEBYTECODE=1
/usr/bin/python3 scripts/prepare_smoke.py "$run_dir"
module load dorado/2.1.0
dorado download --model dna_r10.4.1_e8.2_400bps_hac@v6.0.0 \
    --models-directory "$run_dir/models"
printf '%s\n' "$run_dir/models/dna_r10.4.1_e8.2_400bps_hac@v6.0.0" > model.txt

sbatch --job-name=haplocaller-smoke --account=viseg --partition=gpu \
    --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G \
    --gres=gpu:tesla:1 --time=00:15:00 \
    --chdir="$run_dir" --output="$run_dir/details/slurm.log" \
    "$run_dir/scripts/pangloss-smoke.sh" "$run_dir"
```

Submission returns a job ID; the job runs independently of the SSH connection. Check it with `squeue -j JOB_ID`, read `$run_dir/status.txt`, or follow `$run_dir/run.log`. On failure, the readable log identifies the failed stage and its detailed log file. There is no automatic resume implementation yet; the basecalling script refuses to overwrite an existing `calls` directory.

## Two GPUs on one node

Use a fresh SSD run directory prepared as above, then submit:

```bash
sbatch --job-name=haplocaller-dual --account=viseg --partition=gpu \
    --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=16G \
    --gres=gpu:tesla:2 --time=00:15:00 \
    --export=ALL,HAPLOCALLER_EXPECTED_GPUS=2,HAPLOCALLER_BATCHSIZE=64 \
    --chdir="$run_dir" --output="$run_dir/details/slurm.log" \
    "$run_dir/scripts/pangloss-smoke.sh" "$run_dir"
```

Slurm allocates two GPUs on the same node and sets `CUDA_VISIBLE_DEVICES`. The script preserves that mask and launches **one** Dorado process with `--device cuda:all`. Dorado distributes the basecalling work internally; the wrapper does not split POD5 files or launch a second copy of the basecaller. Multiple nodes would require a separate workflow with disjoint inputs; increasing `--ntasks` is not the same operation.

The fixed batch size of 64 chunks is only for this small functional test. Omit `HAPLOCALLER_BATCHSIZE` (or set it to `0`) for Dorado's automatic selection. GPU count and batch size are recorded in `details/allocation.json`; the script checks that the visible-device count matches the request. `details/gpu-usage.csv` records utilization once per second, and `details/gpu-processes.csv` identifies which process uses each GPU. These monitoring files are diagnostic evidence and do not replace the output verification.

See [Dorado's device selection documentation](https://software-docs.nanoporetech.com/dorado/latest/#specifying-hardware-resources) for how `cuda:all` respects `CUDA_VISIBLE_DEVICES`.

Validated on Pangloss with job `1158243`: both GPUs were active under the same Dorado PID, all 300 reads and raw signals passed output verification, and a matching one-GPU control (job `1158244`, also batch size 64) gave identical sequences, qualities and barcode assignments. Evidence is saved locally in `../runs/pangloss-dual-20261019.ydoY5g/`. This verifies same-node execution, not production-scale throughput.

Changing batch size between the original automatic-batch run and these fixed-batch runs changed some calls in Dorado 2.1.0. Dorado [2.1.2 lists a HAC v6.0.0 nondeterminism fix](https://github.com/nanoporetech/dorado/blob/release-v2.1/CHANGELOG.md); that version still needs user-approved installation and validation before production use. The installed version has been preserved.

## Verification and boundaries

The first test produced 300 calls, 251 passing Q10 and 49 below Q10. Of all calls, 91 were unclassified. Each of the 300 raw signals was preserved once, with matching signal arrays and checked metadata. Exported BAM and FASTQ records had identical IDs, sequences, and qualities.

Raw archives retain signals independently of basecall quality. A split raw read with disagreeing child classifications is routed to `archive_ambiguous`; a raw read without any basecall is routed to `archive_uncalled`. These branches are implemented but were not exercised by the first run, which produced no split or uncalled reads.

The exporter stores validation data in memory and is intended only for this small smoke test. It must be replaced with a bounded streaming/on-disk approach before using it on the full 1.48 TB dataset. Model tiers, kit selection, output selection, discard modes, updates, restart, and multiple GPU jobs still need the production launcher/workflow. Synthetic test aliases are not biological sample names.

## Candida Plate 3 FAST pilot

`prepare_candida.py` and `pangloss-candida-fast.sh` prepare the specific Plate 3 dataset, using the real aliases, the existing 805 GiB POD5 input, two GPUs and FAST v5.2.0. The launcher now defaults to `--pilot-only`: it processes 1,000 reads, exports flat `fastq/ALIAS.fastq.gz` results and stops. Dorado writes plain FASTQ into an internal directory; `flatten_native_fastq.py` merges and compresses batches by sample, and `verify_native_fastq.py` checks the compressed output and split-aware counts. These scripts do not export POD5 and are not the finished interactive launcher.

The original job `1158245` processed the pilot, then stopped because the first verifier incorrectly expected native gzip output. The user then instructed not to submit the full run. No further job was submitted. The existing pilot was packaged and verified under `/ssd_workspace/gbrach/Candida_plate3_fast_test_20261019`, with 1,001 records from 1,000 raw reads in 90 nonempty sample/unclassified files. The original work directory `/ssd_workspace/gbrach/Candida_plate3_fast_20261019.DitIwR` preserves the model and failed-wrapper logs. Full mode requires the explicit `--full` argument and is not currently authorized for submission. The revised launcher itself has syntax checks but has not been resubmitted; its packaging/verification helpers were exercised on the existing pilot. Detailed provenance is in `../docs/cluster-notes.md`.
