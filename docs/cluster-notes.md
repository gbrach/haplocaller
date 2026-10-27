# Dorado workflow: requirements and cluster discovery

Discovery recorded 2026-09-22; status checked 2026-10-20. Pangloss smoke tests successfully basecalled on one and two GPUs and verified BAM, FASTQ.gz, and POD5 exports. A guided `basecall` launcher is now implemented and has completed its own two-GPU FAST pilot; see the [user guide](../README.md). The launcher prepares runs and offers explicit test/full submission, flat FASTQ.gz or BAM exports, saved choices, status/log commands and consent-based private Dorado updates. Multi-node execution, automatic resume and POD5 export in the wizard remain unimplemented.

## Guided wizard validation (2026-10-19)

Current installation after cleanup recovery: `/ssd_workspace/gbrach/.basecall/app`, linked from `~/.local/bin/basecall`. The barcode CSV is restored at `/ssd_workspace/gbrach/barcodes/Candida_plate3_barcodes.csv`, and the saved table choice points there. The original `haplocaller/` installation described below was removed by a user cleanup command; the persistent replacement keeps program files separate from run outputs.

### Full run and live watcher

The user submitted full job `1158248` on 2026-10-19 at 18:32 Europe/Paris. It ran FAST v5.2.0, Q7, two V100S GPUs and the full 10,218,481-read input, with a 14-day requested walltime and no `--max-reads` argument. Its directory is `/ssd_workspace/gbrach/Candida_plate3_fast_20261019`. Dorado exited successfully at 05:16 on October 20; all 97 FASTQs were exported and individually verified before the wrapper failed its incorrect global count check at 06:26 (11:53:53 elapsed).

The corrected accounting follows Dorado release-v2.1's [pipeline ordering](https://github.com/nanoporetech/dorado/blob/release-v2.1/dorado/cli/cli_lib/basecaller.cpp), [filter](https://github.com/nanoporetech/dorado/blob/release-v2.1/dorado/read_pipeline/nodes/ReadFilterNode.cpp), and [writer counters](https://github.com/nanoporetech/dorado/blob/release-v2.1/dorado/hts_writer/HtsFileWriter.cpp). Demultiplexing precedes trimming/internal filtering. With `--output-dir`, Q-score failures are written to native fail files, while short sequences are removed internally. The “Simplex reads basecalled” counter includes only written `subread_id=0` records; filtered additional split segments must not be treated as additional raw inputs.

For this run: 10,232,008 demultiplexed segments = 10,225,054 written records + 6,954 internally filtered segments. Of 10,218,481 raw reads, 10,211,625 have a written first segment, leaving 6,856 filtered first segments and 98 filtered additional segments. This accounting is consistent, but the aggregate logs do not establish the identities of the filtered raw reads. The summary reports 8,844,890 retained Q7 records (62,751,563,595 bases), 1,380,164 Q failures and 515,192 retained unclassified records.

The repair is staged under `.basecall/repairs/counter-check-20261020/`; its previous installation is backed up there. The final code and tests are installed in `.basecall/app/`. All 35 tests pass on Pangloss Python 3.9, including the real full-run counters, zero-valued counters omitted by Dorado, deliberately missing/duplicated/corrupt files, inactive-job guards and matching-job recovery status. Existing FASTQs passed CPU-only revalidation against summary IDs, lengths and per-alias counts. No basecalling rerun or POD5 modification was performed.

Revalidation job `1158253` completed with `COMPLETED|0:0` in **15:20**, using four CPUs, 8 GiB RAM and no GPUs on niobe to read the SSD locally. Two earlier attempts stopped at the inactive-job guard before data scanning. On niobe, `sacct` tries an unavailable database at localhost:6819; `squeue -j` also errors for old expired job IDs. The guard therefore checks the full live job list for the original job ID and only falls back to accounting if the live-list query fails. This avoids a dependency on accounting access from compute nodes.

At **2026-10-20 14:29:16 Europe/Paris**, revalidation completed successfully for **97 files / 8,844,890 retained reads / 62,751,563,595 bases**. Every gzip stream was reread completely; FASTQ structure, sequence/quality lengths, per-sample counts/bases and an order-independent SHA256 fingerprint of read IDs/lengths matched the summary. Input manifests and output file identities remained unchanged during validation. `verification.json` and `sample_counts.tsv` were rebuilt, and workflow state is `complete`. `status` and `watch` explicitly distinguish successful recovery from original job `1158248`'s historical `FAILED|1:0`. Audit evidence, including the old failure state, verifier source snapshot, per-file checks and uncompressed-payload hashes, is under `logs/revalidation-2026-10-20T141356_0200/`. Aggregate raw-parent coverage remains a stated limitation, not an inferred guarantee. Local metadata copies are under `runs/candida-plate3-full-1158248/`.

The installed interface now proposes SUP for each new run, defaults first-time quality filtering to Q7, and remembers the other previously selected values. Terminal headings, selected defaults, warnings and errors have distinct colours; redirected output remains plain, with `--no-color` and `NO_COLOR` supported. These UI defaults do not change existing job configurations or code snapshots.

`basecall watch` reads the live sequencing summary and Slurm state. An incremental SQLite index under `.basecall/watch/` deduplicates parent UUIDs so split reads do not inflate raw-read progress. The viewer displays quality/retention counts, approximate remaining time, free space and final file export progress. It neither reads raw signals/basecall payloads nor writes into the run. Initial history indexing is labelled separately and suppresses ETA. Scheduler failures override stale run states; successful verification is required before reporting completion. A real pseudo-terminal test confirmed coloured, refreshed frames and restoration of the terminal after Ctrl+C, while job `1158248` continued running. All twenty automated checks passed on Pangloss. Validation records are in the installed tool's `validation/` directory and local `runs/watcher-validation-20261019/`.

Installed the tool in `/ssd_workspace/gbrach/haplocaller` with a launcher symlink at `~/.local/bin/basecall` for gbrach. Routine use is `basecall`, `basecall status`, and `basecall logs`; status/log commands default to the most recently prepared run. Installation for other lab accounts is a separate one-time maintainer task.

Following the user's refinements, the wizard accepts **CSV/TSV only**. The Plate 3 mapping was exported separately from `Foglio1!A3:B98` in the supplied workbook to `inputs/Candida_plate3_barcodes.csv` under the Pangloss tool directory. Its 96 unique pairs exactly match the independently extracted mapping used for the original pilot. The workbook remains unchanged, and Plate 5 is excluded.

Requested runtime defaults to **14 days**, is editable, and is remembered. There is no wrapper-imposed upper runtime or GPU-count cap; Slurm enforces resources and policy. Pangloss's `sinfo -p gpu -h -o '%P|%l|%G'` reported `gpu|infinite|gpu:tesla:2`. Unistra's previously observed 24-hour partition limit still matters there. Full mode has no read-count cap; the explicitly selected small test caps raw reads at 1,000.

The real wizard ran with `--prepare-only --no-update-check` against the full Candida input and the new CSV, producing `/ssd_workspace/gbrach/Candida_plate3_wizard_preview_20261019`. It inspected 157 POD5 files / 10,218,481 reads, retained FAST/Q0/unclassified settings, downloaded the matching FAST v5.2.0 model, and prepared a two-GPU / 14-day job configuration. Status is `ready`; neither a submission record nor a submission lock exists. The generated Bash passed syntax validation. No Dorado update or Slurm job was performed.

All nine automated tests passed on Pangloss, including BAM chunk/header merging and checks that preparation/default menu choices never submit. Tests mock scheduler submission. The new exporter also processed the **existing** 1,000-raw-read pilot without invoking Dorado: all 1,001 output records matched the previous verified FASTQs by unique read ID, alias and complete record bytes. Evidence is under `/ssd_workspace/gbrach/haplocaller/validation/`, with copies under local `runs/wizard-validation-20261019/`.

This first validation covered preparation and output handling; the Slurm worker had not yet run. The full Candida run remained unsubmitted and the queue was empty at that point. The automatic private-update installation branch has not been exercised against a newly downloaded Dorado release.

The user subsequently submitted small-test job `1158246` through the wizard, selecting Q7 and two GPUs. It failed during Dorado argument parsing: the launcher sent `--min-qscore 7.0`, but the installed Dorado 2.1.0 requires a decimal integer. No basecalls were produced. The user requested deletion of this failed test; `/ssd_workspace/gbrach/Candida_plate3_fast_20261019` was deleted after verifying the job was `FAILED|1:0`, and its latest-run shortcut was cleared. Source POD5s and the barcode CSV were retained.

Q-score collection and command construction now use integers. Submission also normalises integral floats in previously prepared configurations, so older code snapshots receive `7` or `0` rather than `7.0` or `0.0`. A regression check covers both pilot and full commands, legacy float settings and rejection of fractional thresholds. All ten automated checks passed on Pangloss. No replacement GPU job was submitted by the assistant.

The user then submitted small-test job **1158247**, using the same readable run name after the failed directory was deleted. It completed on `niobe` with `COMPLETED|0:0` in **16 seconds**, allocating two GPUs, 16 CPUs and 64 GiB memory. One Dorado 2.1.0 process ran FAST v5.2.0 with `--device cuda:all`, `--min-qscore 7`, `--max-reads 1000`, batch size 64 and the 96-alias CSV. Slurm exposed devices `0,1`, and Dorado explicitly logged both V100S 32 GB GPUs. This is a functional pilot, not a whole-dataset throughput benchmark.

The pilot processed 1,000 raw reads into 1,001 basecall records. It retained **818** records at Q7 or above, including **37 unclassified**, and discarded **183** for quality. There are **89** nonempty files under `/ssd_workspace/gbrach/Candida_plate3_fast_20261019/fastq/`, with no pass/fail directories in the final result tree. Counts include all 96 aliases plus unclassified, including zero-count rows. An independent pass reopened every gzip stream and confirmed FASTQ structure, unique output IDs, exact record/alias equality with native passing files, and agreement with every counts row. Evidence is in `verification.json` and `logs/independent-verification.json`; metadata/log copies are saved locally under `runs/candida-plate3-wizard-pilot-1158247/`. The full dataset has not been submitted.

## Requirements discussed

- Focus on Pangloss and Unistra, both using Slurm. IGBMC is deferred.
- Accept one POD5 file or a directory, plus a CSV/TSV mapping barcode to alias.
- Ask interactively for output formats (BAM, FASTQ.gz, demultiplexed POD5), unclassified-read retention, Dorado quality filtering, and model tier (fast/hac/sup).
- Make the barcoding kit configurable, with `SQK-NBD114-96` as the usual kit.
- Support one GPU or distributed work, with GPUs per job and a maximum total GPU allocation.
- Check for Dorado updates and ask the user before updating. Pin the selected executable and model versions for a run and its retries.
- Demultiplexed POD5 is for internal raw-signal archival.
- The user interface should be a friendly terminal wizard with a simple readable run log. Proposed behavior includes saved settings, status/resume commands, and jobs that survive SSH disconnection.

The implemented launcher uses Python and a generated Bash Slurm job, without a Snakemake dependency. It snapshots code and settings into each run. The supplied mapping becomes a Dorado sample sheet, which affects barcode selection as well as naming. Keeping all assigned raw signals regardless of basecall Q-score remains a possible future archival policy. Duplex and modified-base calling are outside the first version.

## Unistra

SSH alias: `unistra`; login user: `gbrach`; account: `facvie`.

- GPU partition: `publicgpu`, maximum wall time 24 hours.
- CPU partition: `public`.
- GPU counts are requested with `--gres=gpu:N`.
- Node features identify models, for example `gpua100` and `gpuv100`.
- A bare GPU request could select an older P100, which current Dorado documentation excludes. Use an explicit compatible GPU feature constraint.
- A100-only diagnostic submission was accepted but remained queued. Broadening the constraint to `gpua100|gpuv100` allowed it to run on a V100 node.

Diagnostic job `17975032` completed successfully in 5 seconds on `hpc-n870`:

- GPU: Tesla V100-SXM2, 32 GB.
- NVIDIA driver: 580.105.08.
- `CUDA_VISIBLE_DEVICES=0` for the single-GPU request. `nvidia-smi` lists all four physical GPUs, so its display must not be used to override the scheduler's visibility setting.
- Architecture: x86_64; glibc: 2.28; Python: 3.12.11.
- Log: `/home2020/home/facvie/gbrach/dorado-check.bVDqAj/check.log`.

Accepted diagnostic resource arguments (these are not production CPU/memory defaults):

```bash
--account=facvie --partition=publicgpu \
--constraint='gpua100|gpuv100' --gres=gpu:1 \
--nodes=1 --ntasks=1 --cpus-per-task=2 --mem=4G --time=00:02:00
```

No Dorado or Snakemake executable was found on the default SSH PATH, and no Dorado module was listed by the module query. Apptainer and Singularity modules are available. Basecalling itself has not been tested on this host.

## Pangloss

SSH alias: `pangloss` (two s characters); login user: `gbrach`; account: `viseg`.

- User requirement (2026-10-19): GPU jobs must use `/ssd_workspace/gbrach`. Stage input data, downloaded models, scripts, temporary/cache files, logs, and outputs there; set the Slurm working directory there. Earlier home-directory diagnostics predate this requirement.
- GPU partition: `gpu`; node: `niobe`.
- Two GPU resources, requested as `--gres=gpu:tesla:N`.
- The diagnostic confirmed Tesla V100S-PCIE GPUs with 32 GB each. Its September 26 log reports NVIDIA driver 580.178.04 and glibc 2.34.
- Both GPUs were allocated during initial discovery, so the diagnostic queued before running.
- CPU partition: `fast`.
- Dorado modules listed: 1.0.0, 1.3.1, 1.4.0, 2.0.0, 2.0.1, 2.1.0.
- `pod5` CLI is available as `/shared/home/gbrach/.local/bin/pod5`, version 0.3.6, using `/usr/bin/python3`. The default Miniforge Python does not import this package.
- `pigz` and Singularity are available.

Diagnostic job `1154957` completed successfully on 2026-09-26 on `niobe` in 21 seconds, with exit code `0:0`. This was verified from Slurm accounting and its saved log on 2026-10-19. It allocated one GPU (`CUDA_VISIBLE_DEVICES=0`) and successfully loaded `dorado/2.1.0`, reporting `2.1.0+48ab35d`. Python was 3.12.8. This verified GPU allocation and executable startup, not actual basecalling.

Script and completed log:

```text
/shared/home/gbrach/dorado-check.0Bcg9w/check.sh
/shared/home/gbrach/dorado-check.0Bcg9w/check.log
```

Accepted diagnostic resource arguments:

```bash
--account=viseg --partition=gpu --gres=gpu:tesla:1 \
--nodes=1 --ntasks=1 --cpus-per-task=2 --mem=4G --time=00:02:00
```

## Test dataset

The user authorized copying a small test set from:

```text
/mnt/viseg/RawReads/DNAseq/PromethION2/20250424_PBE25123_Bretts_plate12/bretts_plate12_pod5
```

That source contains 97 POD5 files, approximately 1.48 TB, already divided by barcode. To keep transfer and testing small, 100 raw reads were extracted from each of the barcode20, barcode66, and unclassified files into new POD5s: 300 raw reads total. Selection uses the first 100 reads, so it is suitable for software smoke tests, not representative performance or biological comparisons.

The original dataset README mentions corrected sample aliases and advises care with sample names. The nearby `rename_run12_samplesheet.csv` maps old aliases to corrected aliases; it is not a barcode/alias sheet. No biological sample mapping has been inferred from it.

The test dataset includes `test_aliases.tsv`, mapping barcode01 through barcode96 to explicitly synthetic `test_barcodeNN` aliases, and `manifest.json` recording sources and counts. These labels must not be used as biological sample names.

Locations:

```text
Pangloss: /shared/home/gbrach/dorado-check.0Bcg9w/testdata/
Local: /Users/brachg/Projects/haplocaller/testdata/pangloss-smoke/
Unistra: /home2020/home/facvie/gbrach/haplocaller-test/testdata/
```

Five files totaling 7,690,739 bytes were transferred through the local workspace to Unistra. SHA-256 checksums match at all three locations. Local test data is excluded by `.gitignore`.

```text
8d6ffc3fc41245bbbc6380ca6f6aa4b60b796dca99353076682536f107d55fb0  manifest.json
451ab25de532e7dfdbec52c75c63625ad4ce5b2f4a1d6b55a31ddcb3d2ab250b  pod5/subset_PBE25123_barcode20_YJS5461.pod5
4e797899bca0edc13701e283f1e918485d09a12d596d8bb84fdbc0c5501b3639  pod5/subset_PBE25123_barcode66_YJS8147.pod5
a52591f9d6ce3fb8d84267e3f18e7cf55a772c79ad5657d0979cb55a8c96898b  pod5/subset_PBE25123_unclassified_unclassified.pod5
99613658517d86b6e24c051eda590df1c5aa0114b587f7f9e8fccc72fd1a77f3  test_aliases.tsv
```

## Remaining validation

- Choose and install or reuse a pinned Dorado version on Unistra. No software was installed or updated during discovery.
- Repeat end-to-end validation on Unistra.
- Exercise raw-signal export with actual Dorado split reads and conflicting child barcode assignments. The implementation handles these cases, but the first 300-read run produced none.
- Validate multiple jobs without duplicated inputs and with predictable restart behavior.

## Successful Pangloss smoke test (2026-10-19)

Job `1158242` completed with exit code `0:0` on `niobe` in 2 minutes 9 seconds. It requested one GPU, four CPUs, and 16 GB RAM for at most 15 minutes. Slurm recorded peak host memory of `1145480K` for the batch step.

All input, model, script, temporary/cache, log, and output files used by this GPU job were under:

```text
/ssd_workspace/gbrach/haplocaller-smoke-20261019.lIGvW1
```

The existing Dorado 2.1.0 module was used without a software update. The downloaded model was resolved and pinned as `dna_r10.4.1_e8.2_400bps_hac@v6.0.0`. Barcoding used `SQK-NBD114-96` and a native sample sheet built from the POD5 experiment/flowcell metadata plus the synthetic barcode aliases.

Results:

| Measure | Count |
| --- | ---: |
| Raw input reads | 300 |
| Basecalled records | 300 |
| Passing Q10 | 251 |
| Below Q10, saved separately | 49 |
| Unclassified, retained (overlaps quality counts) | 91 |
| Raw signals archived and verified | 300 |
| Split children / ambiguous raw parents / uncalled raw parents | 0 / 0 / 0 |

Dorado performed its first-use batch-size tuning: the basecaller command took 118 seconds, while Dorado reported 12.623 seconds for the processing pipeline itself. This deliberately small, nonrandom sample is not a throughput or biological accuracy benchmark.

The export verifier reopened BAM and FASTQ.gz files and checked read IDs, sequences, qualities, grouping, and raw-parent associations. It reopened the demultiplexed POD5 files and checked unique complete coverage, raw-signal hashes, and core read/run metadata against the input. Each input raw read is retained exactly once regardless of Q-score. The test-only policy retains unclassified calls and stores low-quality calls separately; it does not implement the full user-choice interface yet.

Reproducible test code is in `scripts/`; usage and limitations are documented in `scripts/README.md`. Copies of the results and logs are saved locally under `runs/pangloss-smoke-20261019.lIGvW1/` (excluded from Git). The readable log is `run.log`; `results/verification.json` and `results/sample_counts.tsv` contain detailed counts and provenance.

## Successful two-GPU execution (2026-10-19)

Job `1158243` completed on `niobe` with exit code `0:0` in 35 seconds, requesting two V100S 32 GB GPUs on one node, one task, four CPUs and 16 GB host RAM. Its run folder is `/ssd_workspace/gbrach/haplocaller-dual-20261019.ydoY5g`; results and diagnostics are copied locally to `runs/pangloss-dual-20261019.ydoY5g/`.

The same installed Dorado 2.1.0 and HAC v6.0.0 model were reused. Slurm set `CUDA_VISIBLE_DEVICES=0,1`. One Dorado process (PID `1446345`) ran with `--device cuda:all`; Dorado logged both CUDA devices. Monitoring sampled the actual processing interval, excluding startup, and found GPU activity on both cards: 22 of 22 samples on GPU 0 and 19 of 22 on GPU 1. Peak sampled utilization was 33% and 31%. Both devices held allocations for the same process. Evidence is in `details/gpu-verification.json`, with underlying per-second samples and process IDs alongside it.

This small functional test used `--batchsize 64` to feed work to both devices. The command took 31 seconds and the processing pipeline 21.468 seconds. It cannot be used to claim a speedup over the first single-GPU run, which used automatic batch sizing. The wrapper does not divide files or start multiple basecallers: Dorado handles the work distribution inside one process. Multi-node operation remains separate work.

All 300 input reads produced one basecall each, with 251 passing Q10 and 49 below Q10. All BAM/FASTQ and raw-signal verification passed, with no duplicated or missing raw reads. There were 92 unclassified calls, compared with 91 in the earlier single-GPU run; a matching fixed-batch-size control is recorded separately below. Output validity does not imply byte-identical basecalls across runs.

The one-GPU control, job `1158244`, used the same batch size of 64, model, input, kit and sample sheet under `/ssd_workspace/gbrach/haplocaller-control-20261019.Yv773l`. It passed all output checks. Compared by read ID, its 300 sequences, quality strings, barcode assignments and Q10 pass/fail groups were **identical** to the two-GPU run. The comparison is saved in `details/run-comparison.json` in both run folders.

Both fixed-batch runs differed from the earlier automatic-batch run in 129 read sequences and all 300 quality strings; one read changed from barcode66 to unclassified, with no Q10 group changes. These counts measure reads with any difference, not the number or magnitude of base errors. Thus, the observed changes are not specific to using two GPUs. Dorado's [2.1.2 release notes](https://github.com/nanoporetech/dorado/blob/release-v2.1/CHANGELOG.md) list a fix for nondeterminism in HAC v6.0.0, consistent with this reproducibility concern. Its role here has not been established by retesting 2.1.2. Production validation should cover that fixed version after the user approves an update; no software update was performed in these tests.

## Candida Plate 3 FAST pilot; full run on hold (2026-10-19)

User requested a real two-GPU FAST run, then clarified that basecalls should be demultiplexed but POD5 files should not be exported. Job `1158245` was submitted with two GPUs, 16 CPUs, 64 GiB host RAM and a 24-hour time limit. It processed only the 1,000-read pilot before the wrapper stopped on an output-format assumption. **The user subsequently instructed not to submit yet, allowing only the test. No further job was submitted, and the full input never started.** The user also requested no pass/fail result folders and questioned the random directory suffix; final pilot outputs now have a readable folder name and flat sample filenames.

```text
Input:         /ssd_workspace/gbrach/candida_pod5/Candida_plate3
Work/provenance: /ssd_workspace/gbrach/Candida_plate3_fast_20261019.DitIwR
Pilot results: /ssd_workspace/gbrach/Candida_plate3_fast_test_20261019
```

The input manifest contains 157 POD5 files, 861,095,550,416 bytes and 10,218,481 reads. Metadata sampled from the first read in every file agrees on experiment `Candida_Plate3`, flowcell `PBM22663`, kit `SQK-NBD114-96`, product `FLO-PRO114M` and sample rate 5000 Hz. Input files remain in place and are not duplicated. File paths, sizes and modification times are checked again at job startup.

The supplied Excel workbook `/Users/brachg/Downloads/Plate_3_5_organization_barcode.xlsx` was read without modification. Its `Foglio1!A3:B98` range, under the `Plate_3` heading in `B2`, contains all 96 unique barcodes and unique aliases. For example barcode01 maps to YJS10960. Columns C:D belong to Plate 5 and are excluded. The extracted mapping, workbook checksum and source-cell provenance are saved in the run's `config/` directory. The native sample sheet uses the actual POD5 experiment/flowcell metadata.

The pilot reused installed Dorado 2.1.0 and the matching `dna_r10.4.1_e8.2_400bps_fast@v5.2.0` model. One process used `cuda:all` with Slurm's two-device visibility mask, batch size 64 and a 1,000-read limit. It performed inline barcode classification and default trimming, retaining unclassified reads and using Q0 (no quality cutoff). There was no raw POD5 export or BAM conversion. This Dorado version emits plain `.fastq` files despite the output-specification naming examples, so the original wrapper's expectation of native `.fastq.gz` was wrong. The existing pilot output was subsequently compressed and verified without running another GPU job. The original failed job and logs are preserved as provenance.

Verified pilot output is `Candida_plate3_fast_test_20261019/fastq/<alias>.fastq.gz`, including `unclassified.fastq.gz`. It contains 1,001 output records from 1,000 raw reads, with 170 unclassified records and 90 nonempty sample/unclassified files. All 96 named aliases, including zero-count samples, are listed in `sample_counts.tsv`. The final result tree has no pass/fail folders. Dorado's original nested files remain in the work/provenance directory.

The updated launcher defaults to `--pilot-only` and exits after validation. `--full` is a separate explicit mode, **not currently authorized for submission**. `flatten_native_fastq.py` merges each alias's native batches into one compressed file, retaining intermediates. `verify_native_fastq.py` uses bounded memory to read compressed FASTQs, checking gzip CRCs, record structure, sequence/quality lengths, known sample names and the demultiplexed-record count in Dorado's log. It separately checks the raw-read count, since split reads increase the output count. The earlier in-memory POD5 exporter is not used. The wrapper refuses to overwrite existing outputs and does not implement automatic resume. Future full-run and updated launcher execution remain untested; only packaging and verification of the existing pilot were performed after the user changed the scope.
