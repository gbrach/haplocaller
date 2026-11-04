# Haplocaller

A guided terminal wizard for DNA basecalling with Dorado on Slurm.

## Everyday use

On an account where the tool has been installed:

```bash
ssh pangloss
haplocaller
```

The wizard asks for:

1. A POD5 file or folder **on the cluster**. Tab completes paths.
2. A CSV or TSV barcode table with `barcode` and `alias` columns.
3. FAST, HAC or SUP; the barcode kit; FASTQ.gz or unaligned BAM. **SUP is proposed for each new run.**
4. Minimum Q score (`0` keeps all qualities), whether to keep unclassified reads, the number of GPUs and requested runtime (default **14 days**).
5. A readable run name.

It checks for Dorado updates and asks before installing a private copy. Declining keeps the installed version. Inputs and routine choices are remembered for next time. First-use defaults are SUP, Q7, FASTQ.gz, keep unclassified, the cluster's usual GPU count, and 14 days. The model starts at SUP for each new run; other choices reuse your previous settings when available. This does not change any existing run.

Menus highlight the default in green, headings in cyan, warnings in yellow and errors in red. Colours are automatic in a terminal and omitted in redirected output. Use `--no-color` or the `NO_COLOR` environment variable to disable them; `--color always` explicitly enables them.

Review the displayed input, sample count, settings and exact output paths. Then choose:

```text
1. Prepare only — submit nothing             [default]
2. Submit small test (up to 1,000 reads)
3. Submit full dataset
```

Pressing Enter prepares the run without submitting. Preparation downloads the matching model and copies POD5s into the GPU workspace if necessary; the preview shows any copy required. If you choose a test or full run, the wizard prepares it and submits it to Slurm. You can then close SSH.

```bash
haplocaller status       # state and scheduler information for the latest prepared run
haplocaller watch        # live progress dashboard; Ctrl+C leaves the job running
haplocaller logs         # follow its short log; Ctrl+C stops watching
haplocaller start        # revisit the submission menu for a prepared, unsubmitted run
haplocaller cancel       # asks before cancelling its Slurm job
haplocaller clean        # reduce a verified, completed run to results, logs and config
```

Each command also accepts a run folder to select an older run. A test does **not** automatically launch the full dataset. After a successful test, run `haplocaller` again, accept or change the remembered settings, give the full run its own name, and choose **Submit full dataset**.

## Live progress

`haplocaller watch` shows the job's stage, raw-read progress bar, quality counts, retained unclassified records, elapsed time, an approximate ETA, free workspace and verified final-file count. It refreshes every three seconds and checks Slurm at most once every ten seconds. It exits when the run finishes or fails. Ctrl+C only closes the viewer.

```bash
haplocaller watch --interval 5
haplocaller watch --once
haplocaller watch /ssd_workspace/USER/another_run
```

The watcher works with existing running jobs: it reads Dorado's growing `sequencing_summary.txt` without modifying the run or opening POD5/FASTQ data. Distinct parent read IDs avoid counting split reads twice in the progress bar. Record/quality counts still count each basecall record. Buffered summary writes may lag behind computation.

The first view may show **Reading progress history** while catching up. A SQLite index under `.basecall/watch/` makes subsequent updates incremental and keeps RAM use bounded. On large runs the index can occupy hundreds of megabytes. ETA appears only after the index catches up and is an average-based estimate, not a prediction from read lengths. It is hidden if summary growth has stalled. Basecalling reaching 100% is followed by file export and verification; the watcher reports success only when the workflow has completed verification. Unsupported summary formats show an explanatory message instead of an invented percentage. Piped output automatically gives one plain-text snapshot.

## Results

For example, choosing `Candida_plate3_fast_full` on gbrach's Pangloss account gives:

```text
/ssd_workspace/gbrach/Candida_plate3_fast_full/
├── fastq/
│   ├── YJS10960.fastq.gz
│   ├── YJS11072.fastq.gz
│   └── unclassified.fastq.gz
├── config/
│   ├── aliases.tsv
│   ├── sample_sheet.csv
│   └── run.json
└── logs/
    ├── sample_counts.tsv
    ├── verification.json
    ├── run.log
    └── ...
```

BAM output uses `bam/<alias>.bam` instead. Only samples with retained records get output files; the counts table includes zero-count samples. Unclassified output is created only when requested and nonempty. Final results have no pass/fail folders. Filtering uses Dorado's Q-score classification; discarded-quality and unclassified counts are recorded separately without double counting.

Verification accounts for Dorado's internal filtering separately from the Q-score pass/fail files. Its demultiplexed count must equal written records plus internally filtered segments. Its “Simplex reads basecalled” counter counts written first segments, so it need not equal the raw POD5 count when filtering and splitting occur. The report checks the appropriate bounds instead of assuming equality. Report schema 2 names these quantities `input_raw_reads`, `dorado_primary_reads_written`, `dorado_filtered_records`, and `basecall_records`. Aggregate counters cannot prove complete raw-parent coverage because Dorado does not list the internally filtered IDs; this limitation is recorded in the report.

After successful output verification, the final run folder contains only `fastq/` or `bam/`, `logs/` and `config/`. Cleanup removes `tmp/` (including native FASTQs/BAMs, the sequencing summary and caches), `models/`, `code/`, the job script and submission lock. A staged `input/` copy is removed only after confirming that the original POD5s still exist with the recorded sizes and modification times. Original source data is never removed. Dorado/model versions, settings and the naming table remain in `config/`; counts, verification and scheduler records remain in `logs/`. Failed runs keep their working files. Raw POD5 files are not demultiplexed by this version.

For older completed runs, use `haplocaller clean /path/to/run`. It moves top-level reports into `logs/` and removes the working files. `status`, `watch` and `logs` support both layouts. Cleanup requires successful verification, an unchanged final output file set, matching count totals and final-file timestamps predating the verification report. It refuses an active Slurm job, symbolic links in cleanup folders, or an unexpected raw POD5 in a temporary folder. It uses the existing verification result; it does not re-read every final sequence file. Removed paths and bytes are recorded in `logs/cleanup.json`.

## Barcode tables

CSV/TSV headers are `barcode,alias` (or `barcode` and `sample`). For example:

```csv
barcode,alias
barcode01,YJS10960
barcode02,YJS11072
```

Barcodes and aliases must be unique. Aliases use 1–40 ASCII letters, digits, underscores or hyphens; `unclassified` is reserved. The wizard accepts CSV/TSV only: export the chosen Excel plate to two columns beforehand. The table must be on the cluster before starting the wizard; a Mac path cannot be used from an SSH session.

One input must represent one sequencing run and compatible chemistry. The wizard samples metadata from the first read of every nonempty POD5 file. It does not exhaustively check run metadata or read-ID uniqueness inside each file. The sample sheet is passed to Dorado; it participates in barcode selection as well as naming, so include every barcode expected in the pool.

## One-time installation and cluster settings

The launcher uses Python 3.9+ and the standard library. The configured processing Python needs `pod5`, plus `pysam` for BAM output. Dorado, Slurm and an appropriate NVIDIA driver must be available on the cluster. No Snakemake installation is required.

The private repository is [gbrach/haplocaller](https://github.com/gbrach/haplocaller). Labmates need repository access and their own GitHub authentication on the cluster.

On Pangloss, install in the account's SSD workspace:

```bash
mkdir -p "/ssd_workspace/$USER/.basecall"
git clone git@github.com:gbrach/haplocaller.git "/ssd_workspace/$USER/.basecall/app"
cd "/ssd_workspace/$USER/.basecall/app"
python3 install.py
haplocaller setup
haplocaller
```

For an existing installation, update its Git checkout instead of cloning over it. On another cluster, use that cluster's working directory and configure it with `haplocaller setup`.

The installer links `~/.local/bin/haplocaller` to `bin/haplocaller` in that folder. Keep the folder in place. It refuses to replace a different existing command and prints a PATH instruction if needed. Installation requires no administrator access and does not download Dorado or submit a job. Run `haplocaller setup` once per account to check the workspace and cluster settings; it is unnecessary when that account is already configured.

The current gbrach installation is `/ssd_workspace/gbrach/.basecall/app`, with its shortcut at `~/.local/bin/haplocaller`. The old `basecall` shortcut remains compatible. The Plate 3 barcode table is `/ssd_workspace/gbrach/barcodes/Candida_plate3_barcodes.csv`. Saved choices and private Dorado installations retain the existing `.basecall/` locations, so renaming the command preserves previous settings and runs.

`profiles/pangloss.json` contains the tested account/partition/GPU settings for gbrach, with `/ssd_workspace/{user}` as the working root. Other users may need their own account and workspace settings. `haplocaller setup` saves an override in `~/.config/basecall/profile.json`; advanced resource settings can be edited there. A custom profile can also be selected with `haplocaller --profile /path/to/profile.json`.

### Unistra: parallel parts under a 24-hour limit

`publicgpu` jobs are capped at 24 h, so the Unistra profile (`max_days: 1`) splits a full run into **parallel parts**. The wizard asks for GPUs per job and the number of parts (default 4, at most the number of POD5 files). Whole POD5 files are divided into byte-balanced groups. Slurm then runs:

1. a GPU **job array**, one Dorado process per part, each with its own GPUs and the 24 h limit;
2. a CPU **merge job** (`afterok` on the whole array) that concatenates each sample across parts, checks Dorado's counters for every part separately, sums them into the usual verification report, and cleans up.

If any part fails, the merge job is cancelled, finished parts stay on disk, and the status shows the failure. Cancel the other parts with `haplocaller cancel`; there is no automatic resume. Choose enough parts that each finishes within 24 h. The small test always runs as a single job. `watch` sums progress across parts. A dataset with a single huge POD5 file cannot be split by this version.

Unistra setup needs a Python that can import `pod5` and `pysam` (set `python` with `haplocaller setup`) and a Dorado executable (the profile points at `~/Softs/dorado-2.1.1-linux-x64`). The Unistra workflow has not yet been validated end to end. IGBMC is outside the current scope.

### Sharing and updates

From the folder where the repository was cloned, update with:

```bash
git pull --ff-only
```

The installed shortcut follows the updated code automatically; it does not need reinstalling after each pull. Keep personal cluster settings in `haplocaller setup` and run outputs outside the checkout. A maintainer should test changes before updating the branch used by labmates. Each prepared run uses its own code snapshot and model until it finishes, so a later pull changes the program used for new runs. Successful cleanup removes those working copies and retains the recorded settings and versions.

Automatic program-update checks and a `haplocaller update` command are not implemented yet. Dorado's separate update check already asks before installation. The current interface runs directly on the cluster; laptop cluster selection is deferred.

Two GPUs means **one Dorado process on one node**, with `--device cuda:all` and Slurm's GPU visibility mask. Dorado distributes work internally. The wrapper does not duplicate input files or launch one independent basecaller per GPU. Multiple nodes are not supported in this version.

The wizard does not impose an upper GPU count or runtime cap. It requests the chosen resources from Slurm. Its default requested walltime is two weeks; the cluster can impose a lower limit and reject or leave that request pending. Unistra's previously observed publicgpu limit was 24 hours. Full mode has no read-count cap; only the explicitly selected small test stops at 1,000 raw reads.

## Validation and limits

To exercise preparation with submission disabled and leave Dorado unchanged:

```bash
haplocaller --prepare-only --no-update-check
```

Automated checks cover input tables, filtering, flat exports, BAM header merging, and submission controls. Run them with `python3 -m unittest discover -s tests -v`; BAM tests require pysam. Scheduler calls are mocked in these tests.

The guided wizard completed a real two-GPU FAST pilot on Pangloss (job `1158247`, 16 seconds). It processed 1,000 raw reads, produced 1,001 basecall records and retained 818 at Q7 or above in 89 flat FASTQ.gz files. All final records and sample assignments matched the native passing output. All 44 automated checks passed on Pangloss on October 27, including filtered/split-read accounting, rejection of missing or duplicated output, recovery safeguards, progress indexing, queued/failed jobs, colour handling, watcher cancellation and cleanup of completed runs.

Full FAST job `1158248` finished basecalling and export on October 20 but exposed the old counter-equality bug. CPU-only job `1158253` successfully revalidated all 97 existing FASTQs with the corrected accounting: 8,844,890 retained Q7 records, including 515,192 unclassified. Counts, bases, read-ID/length fingerprints and complete output streams passed. The workflow now reports completion after revalidation while retaining the original Slurm failure as history. Earlier pilots also exercised one-GPU operation and BAM/POD5 exports. The wizard's BAM path on a live GPU job, private Dorado installation and the Unistra workflow remain untested. See [cluster notes](docs/cluster-notes.md) for evidence and limitations.

Each run can be submitted once. Failed or interrupted runs are preserved; automatic resume and in-place retry are not implemented. Use a fresh run name. Model and executable versions are recorded, and input paths, sizes, modification times and the Dorado version are rechecked at job startup. Updating Dorado requires an explicit choice, installs privately, and leaves the system installation intact; a newly chosen version may require another pilot.

For an export that stopped specifically at the old read-counter check, a maintainer can run `python -m haplocaller.revalidate RUN --workers 4` inside a CPU Slurm allocation. This only reads existing sequence files and updates reports after successful validation; it does not rerun Dorado. It requires the original job to be inactive, checks the input manifest, rereads every gzip/BAM stream and compares per-sample counts, bases and a SHA256 multiset fingerprint of read IDs/lengths with the sequencing summary. Original failure metadata and verifier code are preserved in `logs/revalidation-*/`. `status` and `watch` distinguish successful revalidation from the original Slurm job's historical failure. This is a targeted repair, not a general resume command.
