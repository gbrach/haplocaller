# Examples by cluster

Both use the same barcode table: a CSV or TSV with `barcode` and `alias` columns.

```text
barcode,alias
barcode01,strain_A
barcode02,strain_B
```

Replace `<you>` and every path with your own. The Pangloss paths are real ones from the Candida plate 3 run; the Unistra paths are placeholders. Install first (see the [README](../README.md)).

## Try the bundled example

The repository includes `example/pod5/` (12 POD5 files, 900 raw reads from a real run, about 49 MB) and `example/barcodes.tsv` (96 barcodes with made-up strain names). Use them for the first **Submit small test**, with kit `SQK-NBD114-96`. On Pangloss, copy the `example` folder under `/ssd_workspace/<you>` first.

## Pangloss

Your data must already be under `/ssd_workspace/<you>`. POD5s are never copied.

```bash
ssh <you>@pangloss.ibmp.unistra.fr
haplocaller
```

| Question | Answer |
| --- | --- |
| POD5 file or folder | `/ssd_workspace/<you>/candida_pod5` |
| Barcode table | `/ssd_workspace/<you>/barcodes/Candida_plate3_barcodes.csv` |
| Model / kit / format | SUP, `SQK-NBD114-96`, FASTQ.gz |
| Minimum Q score, GPUs, days | 7, 2, 14 |

Results land in `/ssd_workspace/<you>/<run name>/fastq/`. Slurm account `viseg`, GPU partition `gpu`.

## Unistra

Jobs are limited to 24 h, so a full run is split into parallel parts (12 by default) and merged.

```bash
ssh <you>@hpc-login.u-strasbg.fr
haplocaller
```

| Question | Answer |
| --- | --- |
| POD5 file or folder | any folder you can read, for example `~/my_pod5` |
| Barcode table | `~/barcodes/plate3.csv` |
| Model / kit / format | HAC, `SQK-NBD114-96`, FASTQ.gz |
| Minimum Q score, GPUs per part | 7, 1 |

Results land in `~/haplocaller_runs/<run name>/fastq/`. Slurm account `facvie`, partition `publicgpu`. No Dorado module exists here, so the installer downloads a private copy.

## After the wizard

Pick **Submit small test** first. Then:

```bash
haplocaller watch        # live progress; Ctrl+C leaves the job running
haplocaller status       # the last lines give the Results and Log paths; cd there
```
