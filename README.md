# Haplocaller

A guided terminal wizard for Dorado basecalling on Slurm clusters (Pangloss, Unistra).

## Install

On the cluster's login node:

```bash
git clone https://github.com/gbrach/haplocaller.git
cd haplocaller
./install.sh
```

The installer uses [uv](https://github.com/astral-sh/uv) for the Python environment, detects the cluster, finds your Slurm account, and downloads Dorado if needed. It needs no admin rights. Keep the cloned folder in place.

If uv is not installed, the installer downloads a pinned, checksum-verified copy into `.tools/` inside the clone (nothing goes to your home directory). It was tested from scratch on Pangloss and Unistra.

### Updates

Once a day, when you start `haplocaller`, it checks GitHub. If there is a newer version it asks `Update now? [y/N]`, then restarts on the new version. It stays silent when you are offline, have edited the program files, or have already been asked today. Skip it with `--no-update-check`, or update by hand with `git pull --ff-only`.

## Use

```bash
haplocaller
```

The wizard asks for:

1. A POD5 file or folder on the cluster. On Pangloss it must already be in your `/ssd_workspace` folder. POD5s are never copied.
2. A CSV/TSV barcode table with `barcode` and `alias` columns.
3. The model (FAST, HAC or SUP), the barcode kit and the output format (FASTQ.gz or BAM).
4. Minimum Q score, number of GPUs and runtime.
5. A run name.

Then choose: prepare only (default), a small test of up to 1,000 reads, or the full dataset. A test never launches the full run by itself.

On Unistra, jobs are limited to 24 h, so a full run is split into parallel parts, then merged.

Worked examples for each cluster, with paths and answers: [docs/examples.md](docs/examples.md). Terminal demo scripts: [docs/demos](docs/demos).

## Commands

```bash
haplocaller status   # state of the latest run
haplocaller watch    # live progress; Ctrl+C leaves the job running
haplocaller logs     # follow the log
haplocaller cancel   # cancel the Slurm job
haplocaller clean    # keep only results, logs and config of a verified run
haplocaller setup    # edit saved cluster settings
```

Each command also accepts a run folder.

## Results

```text
<work dir>/<run name>/
├── fastq/ (or bam/)   one file per sample, plus unclassified
├── config/            sample sheet, settings, versions
└── logs/              counts, verification report
```

## Limits

- A failed run is kept and must be redone under a new name. There is no resume.
- Raw POD5 files are not demultiplexed.
- Multiple nodes are not supported.

Tests: `python3 -m unittest discover -s tests -v`
