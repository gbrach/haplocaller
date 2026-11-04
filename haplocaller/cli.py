import argparse
import csv
import datetime as dt
import getpass
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from . import software, ui
from .common import plan_shards, run_file, UserError, command, human_size, mapping_tables, qscore_integer, slurm_args, timestamp, valid_alias, write_json
from .verification import revalidated

PROJECT = Path(__file__).resolve().parent.parent
USER_PROFILE = Path.home() / '.config/basecall/profile.json'


def ask(label, default=None):
    suffix = f' [{default}]' if default is not None else ''
    value = input(ui.prompt(label + suffix + ': ')).strip()
    return value if value else str(default) if default is not None else ''


def choose(label, options, default=0):
    ui.heading(label)
    for index, option in enumerate(options, 1):
        selected = index == default + 1
        print(ui.paint(f'  {">" if selected else " "} {index}. {option}' + ('  [default]' if selected else ''),
                       'selected' if selected else 'label'))
    while True:
        value = ask('Choose', default + 1)
        if value.isdigit() and 1 <= int(value) <= len(options):
            return int(value) - 1
        ui.notice('Please enter one of the numbers above.')


def yes(label, default=False):
    while True:
        value = ask(label, 'y' if default else 'n').lower()
        if value in ('y', 'yes', 'n', 'no'):
            return value in ('y', 'yes')
        ui.notice('Enter y or n.')


def path_input(label, default=None):
    while True:
        try:
            import readline
        except ImportError:
            readline = None
        if readline:
            old_completer, old_delimiters = readline.get_completer(), readline.get_completer_delims()
            def complete(text, index):
                matches = sorted(glob.glob(os.path.expanduser(text) + '*'))
                return (matches[index] + ('/' if Path(matches[index]).is_dir() else '')) if index < len(matches) else None
            readline.set_completer(complete)
            readline.set_completer_delims('\t\n')
            readline.parse_and_bind('tab: complete')
        try:
            value = ask(label, default)
        finally:
            if readline:
                readline.set_completer(old_completer)
                readline.set_completer_delims(old_delimiters)
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        path = Path(value).expanduser()
        if value and path.exists():
            return path.resolve()
        ui.notice('That path was not found on this computer. Use a path on the cluster where you are running haplocaller.')


def profile_names():
    return sorted(p.stem for p in (PROJECT / 'profiles').glob('*.json'))


def detect_cluster():
    """Name of the bundled profile whose host names match this machine, else None."""
    host = socket.getfqdn().lower()
    matches = [n for n in profile_names()
               if any(h in host for h in json.loads((PROJECT / 'profiles' / (n + '.json')).read_text())['hosts'])]
    return matches[0] if len(matches) == 1 else None


def load_profile(selection=None):
    """Bundled cluster profile plus this account's small overrides (~/.config/basecall/profile.json)."""
    saved = json.loads(USER_PROFILE.read_text()) if USER_PROFILE.is_file() else {}
    if selection and Path(selection).expanduser().is_file():
        profile = json.loads(Path(selection).expanduser().read_text())
    elif selection or 'cluster' in saved:
        name = selection or saved['cluster']
        path = PROJECT / 'profiles' / (name + '.json')
        if not path.is_file():
            raise UserError(f'Unknown cluster "{name}". Available: ' + ', '.join(profile_names()))
        profile = json.loads(path.read_text())
        profile['cluster'] = name
        if not selection or saved.get('cluster') == name:
            profile.update(saved)
    elif saved:  # older installs stored a complete profile
        profile = saved
    else:
        name = detect_cluster() or profile_names()[choose('Cluster', profile_names())]
        profile = json.loads((PROJECT / 'profiles' / (name + '.json')).read_text())
        profile['cluster'] = name
    if profile.get('python') in (None, 'auto'):
        profile['python'] = sys.executable
    if profile.get('work_root'):
        profile['work_root'] = str(Path(profile['work_root'].format(user=getpass.getuser())).expanduser())
    return profile


def setup(selection):
    profile = load_profile(selection)
    ui.heading('Cluster setup')
    print('Press Enter to keep a value. Ask your cluster maintainer if unsure.')
    overrides = json.loads(USER_PROFILE.read_text()) if USER_PROFILE.is_file() else {}
    overrides['cluster'] = profile.get('cluster', overrides.get('cluster'))
    for key, label in [('work_root', 'Working directory for GPU jobs'), ('account', 'Slurm account'),
                       ('partition', 'GPU partition'), ('python', 'Python with pod5 and pysam'),
                       ('dorado_executable', 'Dorado executable or absolute path'),
                       ('dorado_module', 'Dorado module (blank if unused)')]:
        overrides[key] = ask(label, profile.get(key) or '') or None
    if not overrides['cluster']:
        overrides.pop('cluster')
    USER_PROFILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(USER_PROFILE, overrides)
    ui.notice(f'Saved {USER_PROFILE}. Start a run with haplocaller.', 'ok')


def check_profile(profile):
    if not profile.get('work_root') or not profile.get('dorado_executable'):
        raise UserError(profile.get('setup_note', 'This cluster needs one-time setup.') + '\nRun ./install.sh in the haplocaller folder (or haplocaller setup).')
    if not shutil.which('sbatch'):
        raise UserError('Slurm is not available here. Connect to the cluster first, then run haplocaller.')
    root = Path(profile['work_root'])
    if not root.is_dir() or not os.access(root, os.W_OK):
        raise UserError(f'The GPU working directory is not writable: {root}\nAsk your cluster maintainer to create it, or run haplocaller setup.')


def read_input(profile, source):
    ui.notice('Checking POD5 files and run metadata…', 'accent')
    result = command([profile['python'], str(PROJECT / 'haplocaller/probe.py'), str(source)],
                     stdout=subprocess.PIPE)
    if result.returncode:
        raise UserError('Could not inspect the POD5 input. See the message above.')
    return json.loads(result.stdout)


def select_software(profile, skip_check):
    selected_profile = profile
    preference = Path(profile['work_root']) / '.basecall/software.json'
    if preference.is_file():
        saved = json.loads(preference.read_text())
        if Path(saved.get('executable', '')).is_file():
            selected_profile = dict(profile, dorado_executable=saved['executable'], dorado_module=None)
    current = software.installed(selected_profile)
    ui.notice(f'Dorado {current["version"]}', 'accent')
    if not skip_check:
        try:
            latest = software.latest_version()
        except Exception:
            ui.notice('Update check unavailable; the installed version will be used.')
        else:
            if software.newer(latest, current['version']):
                if yes(f'Dorado {latest} is available. Install a private copy for this run?'):
                    current = {'version': latest, 'install_version': latest, 'module': None, 'executable': None}
    return current


def preview(config):
    ui.heading('REVIEW YOUR RUN')
    ui.field('Run', config['name'])
    ui.field('Input', config['probe']['input'])
    ui.field('Dataset', f'{config["probe"]["reads"]:,} reads in {len(config["probe"]["files"])} POD5 files ({human_size(config["probe"]["bytes"])})')
    ui.field('Samples', f'{len(config["mapping"])} aliases from {config["mapping_label"]}')
    ui.field('Calling', f'{config["tier"].upper()}, Dorado {config["software"]["version"]}, {config["kit"]}')
    parts = config.get('parts', 1)
    if parts > 1:
        ui.field('Resources', f'{config["profile"]["name"]}: {parts} parallel parts × {config["gpus"]} GPU(s), then a CPU merge job')
        ui.field('Splitting', f'whole POD5 files, balanced by size, about {human_size(config["probe"]["bytes"] / parts)} per part (small test uses one job)')
    else:
        ui.field('Resources', f'{config["profile"]["name"]}, {config["gpus"]} GPU(s) on one node')
    ui.field('Runtime', f'{config["runtime_days"]} days requested' + (' per part' if parts > 1 else '') if config.get('runtime_days') else config['profile']['time'])
    ui.field('Quality', 'keep all reads' if config['min_qscore'] == 0 else f'keep Q ≥ {config["min_qscore"]:g}')
    ui.field('Unclassified', config['unclassified'])
    ext, folder = ('fastq.gz', 'fastq') if config['output'] == 'fastq' else ('bam', 'bam')
    ui.field('Results', f'{config["run_dir"]}/{folder}/<sample>.{ext}')
    ui.field('Counts / log', f'{config["run_dir"]}/logs/sample_counts.tsv and logs/run.log')
    if config['stage_input']:
        ui.field('Preparation', f'copy {human_size(config["probe"]["bytes"])} of POD5s into the GPU working directory')
    ui.field('POD5 export', 'none')
    print(ui.paint('  Small test: up to 1,000 raw reads. Full mode: the entire input.', 'muted'))


def collect(profile, args):
    check_profile(profile)
    previous = Path(profile['work_root']) / '.basecall/last_choices.json'
    last = json.loads(previous.read_text()) if previous.is_file() else {}
    ui.heading(f'HAPLOCALLER  /  {profile["name"]}')
    print(ui.paint('Tab completes paths. Enter accepts the highlighted default. Ctrl+C exits.\n', 'muted'))
    source = path_input('POD5 file or folder', last.get('input'))
    info = read_input(profile, source)
    ui.notice(f'Found {info["reads"]:,} reads; {human_size(info["bytes"])}; kit {info["kit"]}.', 'ok')
    while True:
        table = path_input('Barcode table (.csv or .tsv)', last.get('table'))
        try:
            tables = mapping_tables(table)
            break
        except UserError as error:
            ui.notice(str(error), 'error')
    mapping = tables[0]['rows']
    print(f'{len(mapping)} samples. Examples: ' + ', '.join(f'{r["barcode"]} → {r["alias"]}' for r in mapping[:3]))
    tier = ('fast', 'hac', 'sup')[choose('Basecalling model', ['FAST — quickest', 'HAC — higher accuracy', 'SUP — highest accuracy, slowest'],
                                       ('fast', 'hac', 'sup').index(profile.get('default_model', 'sup')))]
    kit = ask('Barcode kit', info['kit'] or 'SQK-NBD114-96').upper()
    if not valid_alias(kit):
        raise UserError('Invalid kit name.')
    output = ('fastq', 'bam')[choose('Output', ['FASTQ.gz — one file per sample', 'BAM — one unaligned file per sample'],
                                    ('fastq', 'bam').index(last.get('output', 'fastq')))]
    while True:
        try:
            qscore = qscore_integer(ask('Minimum Q score (whole number; 0 keeps all reads)', last.get('min_qscore', profile.get('default_min_qscore', 7))))
            break
        except UserError as error:
            ui.notice(str(error), 'error')
    unclassified = ('keep', 'discard')[choose('Unclassified reads', ['Keep in an unclassified file', 'Discard'],
                                             ('keep', 'discard').index(last.get('unclassified', 'keep')))]
    while True:
        gpus = ask('GPUs per job' if profile.get('max_days') else 'GPUs', last.get('gpus', profile['default_gpus']))
        if gpus.isdigit() and int(gpus) >= 1:
            gpus = int(gpus)
            break
        ui.notice('Enter a positive number of GPUs. Slurm determines what your account can allocate on one node.')
    max_days = profile.get('max_days')
    parts = 1
    if max_days:
        files = len(info['files'])
        print(ui.paint(f'This cluster limits each job to {max_days} day(s). The full dataset is split into parallel parts, '
                       'each its own job with its own GPUs, then merged. Use enough parts that each finishes in time.', 'muted'))
        while True:
            parts = ask('Parallel parts', min(last.get('parts', profile.get('default_parts', 4)), files))
            if parts.isdigit() and 1 <= int(parts) <= files:
                parts = int(parts)
                break
            ui.notice(f'Enter a whole number from 1 to {files} (parts are made of whole POD5 files).')
    while True:
        days = ask('Requested runtime in days' + (' per part' if parts > 1 else ''), min(last.get('runtime_days', 14), max_days or 14))
        if days.isdigit() and int(days) >= 1 and (not max_days or int(days) <= max_days):
            days = int(days)
            break
        ui.notice('Enter a positive whole number of days' + (f', at most {max_days} on this cluster.' if max_days else '. Cluster time limits still apply.'))
    profile = dict(profile, time=f'{days}-00:00:00')
    default_name = re.sub(r'[^A-Za-z0-9_-]+', '_', source.stem)[:35].strip('_') or 'nanopore'
    default_name += '_' + tier + '_' + dt.date.today().strftime('%Y%m%d')
    root = Path(profile['work_root']).resolve()
    base_name, attempt = default_name, 2
    while (root / default_name).exists():
        default_name = base_name + '_' + str(attempt)
        attempt += 1
    while True:
        name = ask('Run name', default_name)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', name):
            ui.notice('Use letters, digits, underscores or hyphens; start with a letter or digit.')
        elif (root / name).exists():
            ui.notice('That run folder already exists and will be kept. Choose a new name.')
        else:
            break
    selected_software = select_software(profile, args.no_update_check)
    if output == 'bam':
        result = command([profile['python'], '-c', 'import pysam'], capture_output=True)
        if result.returncode:
            raise UserError('BAM output needs pysam in the cluster Python. Ask the profile maintainer to install it once, or select FASTQ.gz.')
    staged = not all(Path(row['path']).is_relative_to(root) for row in info['files'])
    return {'schema_version': 1, 'name': name, 'run_dir': str(root / name), 'profile': profile,
            'probe': info, 'original_input': str(source), 'mapping': mapping, 'mapping_label': tables[0]['label'],
            'mapping_source': str(table), 'mapping_sha256': hashlib.sha256(table.read_bytes()).hexdigest(),
            'tier': tier, 'kit': kit, 'output': output, 'min_qscore': qscore,
            'unclassified': unclassified, 'gpus': gpus, 'parts': parts, 'runtime_days': days, 'software': selected_software,
            'stage_input': staged, 'mode': 'pilot', 'created_at': timestamp()}


def prepare(config):
    run = Path(config['run_dir'])
    profile = config['profile']
    free = shutil.disk_usage(profile['work_root']).free
    needed = config['probe']['bytes'] if config['stage_input'] else 0
    reserve = profile.get('minimum_free_gib', 5) * 1024**3
    if free < needed + reserve:
        raise UserError(f'Not enough free working space: {human_size(free)} available, {human_size(needed + reserve)} needed for staging and reserve.')
    run.mkdir(exist_ok=False)
    for folder in ('config', 'logs', 'tmp', 'code'):
        (run / folder).mkdir()
    write_json(run_file(run, 'status.json'), {'state': 'preparing', 'updated_at': timestamp()})
    write_json(run / 'config/run.json', config)
    try:
        shutil.copytree(PROJECT / 'haplocaller', run / 'code/haplocaller', ignore=shutil.ignore_patterns('__pycache__'))
        with (run / 'config/aliases.tsv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['barcode', 'alias'], delimiter='\t')
            writer.writeheader()
            writer.writerows(config['mapping'])
        if config['stage_input']:
            print('Copying POD5 input to the GPU workspace…', flush=True)
            destination = run / 'input'
            destination.mkdir()
            source = Path(config['probe']['input'])
            for index, row in enumerate(config['probe']['files'], 1):
                path = Path(row['path'])
                try:
                    relative = path.name if source.is_file() else path.relative_to(source)
                except ValueError:  # input folder holds links to files elsewhere
                    relative = Path(f'{index:05d}_{path.name}')
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                row.update(original_path=row['path'], path=str(target), mtime_ns=target.stat().st_mtime_ns)
                print(f'  Copied {index}/{len(config["probe"]["files"])} files', flush=True)
            config['probe']['input'] = str(destination)
        if config.get('parts', 1) > 1:
            config['shards'] = plan_shards(config['probe']['files'], config['parts'])
        with (run / 'config/sample_sheet.csv').open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['kit', 'experiment_id', 'flow_cell_id', 'barcode', 'alias'])
            for row in config['mapping']:
                writer.writerow([config['kit'], config['probe']['experiment'], config['probe']['flowcell'], row['barcode'], row['alias']])
        cache = Path(profile['work_root']) / '.basecall'
        if config['software'].get('install_version'):
            print(f'Installing the requested Dorado {config["software"]["install_version"]}…', flush=True)
            config['software'] = software.install(config['software']['install_version'], cache / 'tools')
            write_json(cache / 'software.json', config['software'])
        print(f'Preparing the matching {config["tier"].upper()} model…', flush=True)
        model_dir = run / 'models'
        model_dir.mkdir()
        environment = dict(os.environ, TMPDIR=str(run / 'tmp'), XDG_CACHE_HOME=str(run / 'tmp/cache'))
        with (run / 'logs/model-download.log').open('w') as stream:
            result = command([config['software']['executable'], 'download', '--model', config['tier'],
                              '--data', config['probe']['files'][0]['path'], '--models-directory', str(model_dir)],
                             config['software'].get('module'), stdout=stream, stderr=subprocess.STDOUT, env=environment)
        if result.returncode:
            raise UserError(f'Model preparation failed. Details: {run}/logs/model-download.log')
        models = [p for p in model_dir.iterdir() if p.is_dir() and (p / 'config.toml').is_file()]
        if len(models) != 1:
            raise UserError('Expected exactly one downloaded basecalling model.')
        config['model_path'] = str(models[0])
        config['model_name'] = models[0].name
        write_json(run / 'config/run.json', config)
        module = config['software'].get('module')
        lines = ['#!/bin/bash -l', 'set -euo pipefail', 'cd ' + shlex.quote(str(run)),
                 'export PYTHONPATH=' + shlex.quote(str(run / 'code')),
                 'export PYTHONDONTWRITEBYTECODE=1', 'export TMPDIR=' + shlex.quote(str(run / 'tmp')),
                 'export XDG_CACHE_HOME=' + shlex.quote(str(run / 'tmp/cache')),
                 'export CUDA_CACHE_PATH=' + shlex.quote(str(run / 'tmp/cuda')),
                 'export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-' + str(profile['cpus']) + '}"']
        if module:
            lines.append('module load ' + shlex.quote(module))
        lines.append('exec ' + shlex.join([profile['python'], '-m', 'haplocaller.worker', str(run)]))
        (run / 'job.sh').write_text('\n'.join(lines) + '\n')
        if config.get('shards'):
            merge = lines[:-1] + ['exec ' + shlex.join([profile['python'], '-m', 'haplocaller.worker', str(run), 'merge'])]
            (run / 'job_merge.sh').write_text('\n'.join(merge) + '\n')
        write_json(run_file(run, 'status.json'), {'state': 'ready', 'message': 'Prepared; no job submitted.', 'updated_at': timestamp()})
        cache.mkdir(parents=True, exist_ok=True)
        write_json(cache / 'last_choices.json', {'input': config.get('original_input', config['probe']['input']),
                   'table': config.get('mapping_source'), 'tier': config['tier'], 'output': config['output'],
                   'min_qscore': config['min_qscore'], 'unclassified': config['unclassified'], 'gpus': config['gpus'],
                   'runtime_days': config.get('runtime_days', 14), 'parts': config.get('parts', 1), 'run': str(run)})
        ui.notice(f'Prepared {run}', 'ok')
        return run
    except BaseException:
        write_json(run_file(run, 'status.json'), {'state': 'preparation_failed', 'message': 'Preparation did not complete; no job submitted.', 'updated_at': timestamp()})
        raise


def submit(run, mode):
    config_path = run / 'config/run.json'
    config = json.loads(config_path.read_text())
    status = json.loads((run_file(run, 'status.json')).read_text())
    if status['state'] != 'ready':
        raise UserError('This run is not ready for submission. Completed or attempted runs are kept; use a new run name.')
    # Older prepared runs stored whole-number scores as floats. Normalise before
    # submission so their snapshotted workers also receive an integer setting.
    config['min_qscore'] = qscore_integer(config['min_qscore'])
    try:
        with (run / 'submission.lock').open('x') as stream:
            stream.write(timestamp() + '\n')
    except FileExistsError:
        raise UserError('Submission was already attempted. Check haplocaller status before doing anything else.')
    config['mode'] = mode
    write_json(config_path, config)
    # Mark before invoking sbatch, so an ambiguous network/process failure cannot
    # lead to an accidental second submission on a retry.
    write_json(run_file(run, 'status.json'), {'state': 'submitting', 'updated_at': timestamp()})
    result = command(slurm_args(config, run / 'job.sh'), capture_output=True)
    (run / 'logs/submission.log').write_text(result.stdout + result.stderr)
    if result.returncode:
        write_json(run_file(run, 'status.json'), {'state': 'submission_failed', 'message': result.stderr.strip(), 'updated_at': timestamp()})
        raise UserError('Slurm did not accept the job.\n' + result.stderr.strip())
    job_id = result.stdout.strip().split(';')[0]
    if not job_id.isdigit():
        raise UserError(f'Slurm returned an unexpected response. Check {run}/logs/submission.log before retrying.')
    record = {'job_id': job_id, 'mode': mode, 'submitted_at': timestamp()}
    write_json(run_file(run, 'submission.json'), record)
    if mode == 'full' and len(config.get('shards') or []) > 1:
        merged = command(slurm_args(config, run / 'job_merge.sh', role='merge', dependency=job_id), capture_output=True)
        (run / 'logs/submission_merge.log').write_text(merged.stdout + merged.stderr)
        merge_id = merged.stdout.strip().split(';')[0]
        if merged.returncode or not merge_id.isdigit():
            command(['scancel', job_id])
            write_json(run_file(run, 'status.json'), {'state': 'submission_failed', 'message': 'The merge job was not accepted; the part jobs were cancelled.', 'updated_at': timestamp()})
            raise UserError('Slurm did not accept the merge job, so the parts were cancelled.\n' + merged.stderr.strip())
        record.update(merge_job_id=merge_id, parts=len(config['shards']))
        write_json(run_file(run, 'submission.json'), record)
        job_id = f'{job_id} ({len(config["shards"])} parallel parts) + merge job {merge_id}'
    print()
    ui.notice(f'Submitted {"small test" if mode == "pilot" else "full run"}: job {job_id}. You can close SSH.', 'ok')
    print(f'Watch progress: haplocaller watch {shlex.quote(str(run))}')
    print(f'Read the log:   haplocaller logs {shlex.quote(str(run))}')
    print('For the most recently prepared run, haplocaller watch, status and logs also work without a path.')


def status(run):
    if not (run_file(run, 'status.json')).is_file():
        raise UserError('This is not a run prepared by haplocaller.')
    state = json.loads((run_file(run, 'status.json')).read_text())
    config = json.loads((run / 'config/run.json').read_text())
    report = json.loads((run_file(run, 'verification.json')).read_text()) if (run_file(run, 'verification.json')).exists() else {}
    label = state['state'].replace('_', ' ')
    if state['state'] == 'submitting' and (run_file(run, 'submission.json')).is_file():
        label = 'submitted'
    ui.heading(config['name'])
    ui.notice(label, 'ok' if state['state'] in ('complete', 'pilot_complete') else 'error' if 'fail' in state['state'] else 'accent')
    if state.get('message'):
        counts = run_file(run, 'sample_counts.tsv').relative_to(run)
        print(state['message'].replace('counts: sample_counts.tsv', f'counts: {counts}'))
    if (run_file(run, 'submission.json')).is_file():
        record = json.loads((run_file(run, 'submission.json')).read_text())
        job = record['job_id']
        print('Slurm job: ' + job + (f' (parts) + merge job {record["merge_job_id"]}' if record.get('merge_job_id') else ''))
        if state['state'] in ('complete', 'pilot_complete') and revalidated(report, job):
            print('Existing output revalidated successfully; the original Slurm result below is historical.')
        if shutil.which('squeue'):
            result = command(['squeue', '-h', '-j', job + (',' + record['merge_job_id'] if record.get('merge_job_id') else ''), '-o', '%i|%T|%R'], capture_output=True)
            if result.stdout.strip():
                print('Scheduler:\n  ' + result.stdout.strip().replace('|', ' — ').replace('\n', '\n  '))
            elif shutil.which('sacct'):
                result = command(['sacct', '-X', '-j', job, '-nP', '--format=State,ExitCode'], capture_output=True)
                if result.stdout.strip():
                    print('Scheduler: ' + result.stdout.strip())
    print('Results: ' + str(run / ('fastq' if config['output'] == 'fastq' else 'bam')))
    print('Log: ' + str(run_file(run, 'run.log')))


def main():
    parser = argparse.ArgumentParser(prog='haplocaller', description='Guided nanopore basecalling on Slurm. Run on the cluster without arguments to start the wizard.')
    parser.add_argument('--profile', help='Cluster name or custom profile JSON')
    parser.add_argument('--prepare-only', action='store_true', help='Prepare files and model, never submit')
    parser.add_argument('--no-update-check', action='store_true', help='Use the installed Dorado without checking for updates')
    parser.add_argument('--color', choices=['auto', 'always', 'never'], default='auto', help='Terminal colour (default: auto)')
    parser.add_argument('--no-color', dest='color', action='store_const', const='never', help='Disable terminal colour')
    parser.add_argument('--once', action='store_true', help='watch: display one progress snapshot')
    parser.add_argument('--interval', type=float, default=3, help='watch: screen refresh in seconds (default: 3)')
    parser.add_argument('command', nargs='?', choices=['setup', 'status', 'watch', 'logs', 'start', 'cancel', 'clean'])
    parser.add_argument('run', nargs='?', type=Path)
    args = parser.parse_args()
    ui.MODE = args.color
    try:
        if (args.once or args.interval != 3) and args.command != 'watch':
            raise UserError('--once and --interval apply to haplocaller watch.')
        if args.prepare_only and args.command is not None:
            raise UserError('--prepare-only applies to the wizard; it cannot be combined with another command. Nothing submitted.')
        if args.command == 'setup':
            setup(args.profile)
            return
        if args.command:
            if args.run is None:
                profile = load_profile(args.profile)
                previous = Path(profile.get('work_root') or '') / '.basecall/last_choices.json'
                saved = json.loads(previous.read_text()) if previous.is_file() else {}
                if not saved.get('run'):
                    raise UserError('No recent run found. Supply its folder: haplocaller ' + args.command + ' /path/to/run')
                args.run = Path(saved['run'])
                if args.command != 'watch':
                    print(ui.paint('Using most recently prepared run: ' + str(args.run), 'muted'))
            run = args.run.expanduser().resolve()
            if args.command == 'status':
                status(run)
            elif args.command == 'watch':
                from .watch import watch
                watch(run, interval=args.interval, once=args.once)
            elif args.command == 'clean':
                from .cleanup import cleanup_run
                from .worker import log
                cleaned = cleanup_run(run)
                log(run, f'Cleanup: {cleaned["files_removed"]} files, '
                         f'{human_size(cleaned["bytes_removed"])}.')
            elif args.command == 'logs':
                if not (run_file(run, 'run.log')).is_file():
                    raise UserError('No run log yet. Use haplocaller status to check whether the job has started.')
                subprocess.run(['tail', '-n', '30', '-f', str(run_file(run, 'run.log'))])
            elif args.command == 'start':
                config = json.loads((run / 'config/run.json').read_text())
                preview(config)
                action = choose('Next action', ['Leave prepared; submit nothing', 'Submit small test (up to 1,000 reads)', 'Submit full dataset'])
                if action:
                    submit(run, 'pilot' if action == 1 else 'full')
            elif args.command == 'cancel':
                record = json.loads((run_file(run, 'submission.json')).read_text())
                jobs = [record['job_id']] + ([record['merge_job_id']] if record.get('merge_job_id') else [])
                if yes(f'Cancel Slurm job {" and ".join(jobs)}?'):
                    command(['scancel', *jobs], check=True)
                    print('Cancellation requested. Check with haplocaller status.')
            return
        config = collect(load_profile(args.profile), args)
        preview(config)
        action = 0 if args.prepare_only else choose('Next action',
            ['Prepare only — submit nothing', 'Submit small test (up to 1,000 reads)', 'Submit full dataset'])
        run = prepare(config)
        if action:
            submit(run, 'pilot' if action == 1 else 'full')
        else:
            ui.notice('No job submitted. When ready: haplocaller start ' + shlex.quote(str(run)), 'accent')
    except (KeyboardInterrupt, EOFError):
        print()
        ui.notice('Stopped. Any previously submitted job continues; use haplocaller watch to check it.')
    except (UserError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(file=sys.stderr)
        ui.notice(str(error), 'error', sys.stderr)
        sys.exit(1)
