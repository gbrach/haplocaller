import csv
import datetime as dt
import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path


class UserError(Exception):
    pass


def run_file(root, name):
    """Find run metadata in logs/, with support for older runs."""
    root = Path(root)
    legacy = root / name
    return legacy if legacy.exists() else root / 'logs' / name


def timestamp():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def write_json(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name + '.', suffix='.partial', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(data, stream, indent=2)
        stream.write('\n')
    temporary.replace(path)


def human_size(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:,.1f} {unit}'
        value /= 1024


def command(args, module=None, **kwargs):
    if module:
        args = ['/bin/bash', '-lc', 'module load ' + shlex.quote(module) +
                ' && exec ' + shlex.join([str(v) for v in args])]
    return subprocess.run(args, text=True, **kwargs)


def valid_alias(value):
    return bool(re.fullmatch(r'[A-Za-z0-9_-]{1,40}', value))


def qscore_integer(value):
    try:
        score = float(value)
        if score.is_integer() and 0 <= score <= 100:
            return int(score)
    except (TypeError, ValueError):
        pass
    raise UserError('Enter a whole-number Q score between 0 and 100; Dorado requires an integer.')


def validate_mapping(rows):
    result = []
    for barcode, alias in rows:
        barcode, alias = str(barcode or '').strip(), str(alias or '').strip()
        if not barcode and not alias:
            continue
        if not re.fullmatch(r'barcode[0-9]{2,3}', barcode):
            raise UserError(f'Invalid barcode "{barcode}". Use names such as barcode01.')
        if not valid_alias(alias) or alias == 'unclassified':
            raise UserError(f'Invalid sample name "{alias}". Use 1–40 letters, digits, underscores or hyphens; unclassified is reserved.')
        result.append({'barcode': barcode, 'alias': alias})
    if not result:
        raise UserError('The selected table has no barcode/sample rows.')
    for key, label in [('barcode', 'barcode'), ('alias', 'sample name')]:
        values = [row[key] for row in result]
        if len(set(values)) != len(values):
            raise UserError(f'The table contains a repeated {label}. Give each barcode its own unique sample name.')
    return sorted(result, key=lambda row: row['barcode'])


def mapping_tables(path):
    if path.suffix.lower() not in ('.csv', '.tsv'):
        raise UserError('Use a CSV or TSV with barcode,alias columns. Export the selected Excel plate to CSV first.')
    with path.open(newline='', encoding='utf-8-sig') as stream:
        sample = stream.read(8192)
        stream.seek(0)
        if not sample.strip():
            raise UserError('The barcode table is empty.')
        delimiter = '\t' if path.suffix.lower() == '.tsv' or '\t' in sample.splitlines()[0] else ','
        reader = csv.DictReader(stream, delimiter=delimiter)
        headers = {str(key).strip().lower(): key for key in reader.fieldnames or []}
        alias_key = next((headers[key] for key in ('alias', 'sample', 'sample_name') if key in headers), None)
        if 'barcode' not in headers or not alias_key:
            raise UserError('The table needs column headers barcode and alias (or sample).')
        rows = [(row[headers['barcode']], row[alias_key]) for row in reader]
    return [{'label': path.name, 'rows': validate_mapping(rows)}]


def plan_shards(files, parts):
    """Split POD5 files into at most `parts` byte-balanced groups (largest first)."""
    parts = max(1, min(int(parts), len(files)))
    groups = [{'bytes': 0, 'files': []} for _ in range(parts)]
    for row in sorted(files, key=lambda r: (-r['bytes'], r['path'])):
        target = min(groups, key=lambda g: (g['bytes'], len(g['files'])))
        target['files'].append(row['path'])
        target['bytes'] += row['bytes']
    return [sorted(g['files']) for g in groups if g['files']]


def slurm_args(config, job_script, test_only=False, role='basecall', dependency=None):
    """role: 'basecall' (one job, or an array when the run is sharded) or 'merge' (CPU only)."""
    profile = config['profile']
    run = Path(config['run_dir'])
    sharded = role == 'basecall' and config.get('mode') == 'full' and len(config.get('shards') or []) > 1
    if role == 'merge':
        partition = profile.get('cpu_partition') or profile['partition']
        args = ['sbatch', '--parsable', '--job-name=haplocaller-merge-' + config['name'],
                '--account=' + profile['account'], '--partition=' + partition,
                '--nodes=1', '--ntasks=1', '--cpus-per-task=' + str(profile['cpus']),
                '--mem=' + profile['memory'], '--time=' + profile.get('merge_time', '1-00:00:00'),
                '--chdir=' + str(run), '--output=' + str(run / 'logs/slurm_merge.log'),
                '--dependency=afterok:' + str(dependency), '--kill-on-invalid-dep=yes']
    else:
        args = ['sbatch', '--parsable', '--job-name=haplocaller-' + config['name'],
                '--account=' + profile['account'], '--partition=' + profile['partition'],
                '--nodes=1', '--ntasks=1', '--cpus-per-task=' + str(profile['cpus']),
                '--mem=' + profile['memory'], '--time=' + profile['time'],
                '--gres=' + profile['gres'].format(gpus=config['gpus']),
                '--chdir=' + str(run)]
        if sharded:
            limit = config.get('concurrent') or 0
            args += ['--array=0-' + str(len(config['shards']) - 1) + (f'%{limit}' if 0 < limit < len(config['shards']) else ''),
                     '--output=' + str(run / 'logs/slurm_%a.log')]
        else:
            args.append('--output=' + str(run / 'logs/slurm.log'))
        if profile.get('constraint'):
            args.append('--constraint=' + profile['constraint'])
    if test_only:
        args.append('--test-only')
    return args + [str(job_script)]
