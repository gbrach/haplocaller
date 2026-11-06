# Examples by cluster

All three use the same barcode table: a CSV or TSV with `barcode` and `alias` columns.

```text
barcode,alias
barcode01,strain_A
barcode02,strain_B
```

Replace every path below with your own. Install first (see the [README](../README.md)).

## Pangloss

Your data must already be under `/ssd_workspace/<you>`. POD5s are never copied.

```bash
ssh pangloss
haplocaller
```

| Question | Answer |
| --- | --- |
| POD5 file or folder | `/ssd_workspace/<you>/my_pod5` |
| Barcode table | `/ssd_workspace/<you>/barcodes/plate3.csv` |
| Model / kit / format | SUP, `SQK-NBD114-96`, FASTQ.gz |
| Minimum Q score, GPUs, days | 7, 2, 14 |

Results land in `/ssd_workspace/<you>/<run name>/fastq/`. Slurm account `viseg`, GPU partition `gpu`.

## Unistra

Jobs are limited to 24 h, so a full run is split into parallel parts (12 by default) and merged.

```bash
ssh unistra
haplocaller
```

| Question | Answer |
| --- | --- |
| POD5 file or folder | any folder you can read, for example `~/my_pod5` |
| Barcode table | `~/barcodes/plate3.csv` |
| Model / kit / format | HAC, `SQK-NBD114-96`, FASTQ.gz |
| Minimum Q score, GPUs per part | 7, 1 |

Results land in `~/haplocaller_runs/<run name>/fastq/`. Slurm account `facvie`, partition `publicgpu`. No Dorado module exists here, so install with `./install.sh --dorado download`.

## IGBMC (phantom)

```bash
ssh phantom
haplocaller
```

| Question | Answer |
| --- | --- |
| POD5 file or folder | any folder you can read |
| Barcode table | a CSV or TSV on the cluster |
| Model / kit / format | SUP, `SQK-NBD114-96`, FASTQ.gz |
| Minimum Q score, GPUs, days | 7, 1, up to 8 |

Results land in `/shared/mendel/projects/ONTcaal/<you>/<run name>/fastq/`. Slurm account `schacherer`, partition `gpu`.

## After the wizard

Pick **Submit small test** first. Then:

```bash
haplocaller watch        # live progress; Ctrl+C leaves the job running
haplocaller status
haplocaller status       # the last lines give the Results and Log paths; cd there
```
