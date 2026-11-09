# Haplocaller

A guided terminal wizard for Dorado basecalling on Slurm clusters (Pangloss, Unistra).

## Install

On the cluster's login node:

```bash
git clone https://github.com/gbrach/haplocaller.git
cd haplocaller
./install.sh
```

No admin rights needed. If the cluster has no Dorado, the installer downloads a private copy. Keep the folder in place. When a new version is available, `haplocaller` offers to update itself at start (it checks GitHub at most once a day).

## Use

```bash
haplocaller
```

The wizard asks for your POD5 folder, a CSV/TSV barcode table (`barcode`, `alias`), and a few options. Defaults: Q7, FASTQ.gz, unclassified reads kept, demultiplexed POD5 on. Then choose to prepare only, run a small test (up to 1,000 reads), or run everything.

POD5 export copies all raw signal, so it needs about as much free disk as your input. The wizard warns you in red.

Try it first with the bundled example (900 reads, 96 barcodes): `example/pod5` and `example/barcodes.tsv`. Examples per cluster: [docs/examples.md](docs/examples.md)

## Commands

```bash
haplocaller status   # state of the latest run
haplocaller watch    # live progress (Ctrl+C leaves the job running)
haplocaller logs     # follow the log
haplocaller clean    # keep only results, logs and config
```

Stop a job with `scancel <job id>`.

## Results

```text
<work dir>/<run name>/
├── fastq/ and/or bam/   one file per sample
├── pod5/                raw signal per sample
├── config/              settings
└── logs/                counts, verification
```

## Limits

- A failed run must be redone under a new name. There is no resume.
- One node per job.
