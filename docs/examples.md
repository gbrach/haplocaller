# Examples by cluster

All three use the same barcode table: a CSV or TSV with `barcode` and `alias` columns.

```text
barcode,alias
barcode01,strain_A
barcode02,strain_B
```

Replace `<you>` and every path with your own. The Pangloss paths are real ones from the Candida plate 3 run; the Unistra and IGBMC paths are placeholders. Install first (see the [README](../README.md)).

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

Results land in `~/haplocaller_runs/<run name>/fastq/`. Slurm account `facvie`, partition `publicgpu`. No Dorado module exists here, so install with `./install.sh --dorado download`.

## IGBMC (phantom)

*Not yet tested: the profile exists but nobody has run an install or a job there.*

Log in to the phantom login node, then:

```bash
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
