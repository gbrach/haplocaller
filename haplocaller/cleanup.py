"""Keep only final sequences, logs and configuration after verification."""
import fcntl
import json
import os
import stat
import subprocess
from pathlib import Path

from .common import run_file, UserError, timestamp, valid_alias, write_json


def cleanup_run(root, check_scheduler=True):
    root = Path(root).resolve(strict=True)
    if (root / "logs").is_symlink() or (root / "config").is_symlink():
        raise UserError("Run metadata folders must not be symbolic links.")
    with (root / "logs/.cleanup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UserError("Cleanup is already running.")
        return _cleanup_run(root, check_scheduler)


def _cleanup_run(root, check_scheduler):
    root = Path(root).resolve(strict=True)
    config = json.loads((root / 'config/run.json').read_text())
    state = json.loads((run_file(root, 'status.json')).read_text())
    report_path = run_file(root, 'verification.json')
    report = json.loads(report_path.read_text())
    if state.get('state') not in ('complete', 'pilot_complete') or report.get('verification') != 'passed':
        raise UserError('Cleanup requires a completed run with verified output.')
    if config['output'] not in ('fastq', 'bam'):
        raise UserError('Unknown output format; temporary files kept.')
    if check_scheduler and (run_file(root, 'submission.json')).is_file():
        submitted = json.loads((run_file(root, 'submission.json')).read_text())
        jobs = {str(submitted['job_id']), str(submitted.get('merge_job_id', ''))} - {''}
        result = subprocess.run(['squeue', '-h', '-o', '%i'], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise UserError('Cannot check Slurm; temporary files kept.')
        if jobs & {line.strip().split('_', 1)[0] for line in result.stdout.splitlines()}:
            raise UserError('The Slurm job is still active; temporary files kept.')

    rows = json.loads((root / 'logs/export_counts.json').read_text())
    allowed = {row['alias'] for row in config['mapping']} | {'unclassified'}
    if (any(not valid_alias(alias) for alias in allowed)
            or any(row['alias'] not in allowed for row in rows)
            or len({row['alias'] for row in rows}) != len(rows)):
        raise UserError('Invalid output manifest; temporary files kept.')
    for field, report_field in [('reads', 'kept_reads'), ('native_records', 'basecall_records'),
                                ('discarded_quality', 'discarded_quality'),
                                ('discarded_unclassified', 'discarded_unclassified')]:
        if sum(row[field] for row in rows) != report[report_field]:
            raise UserError('Output manifest differs from verification; temporary files kept.')

    output = root / config['output']
    suffix = '.fastq.gz' if config['output'] == 'fastq' else '.bam'
    expected = {row['alias'] + suffix for row in rows if row['reads'] > 0}
    if output.is_symlink() or {p.name for p in output.iterdir()} != expected:
        raise UserError('Final output files differ from verification; temporary files kept.')
    verified_at = report_path.stat().st_mtime_ns
    outputs = []
    for name in sorted(expected):
        path = output / name
        identity = path.lstat()
        if (not stat.S_ISREG(identity.st_mode) or identity.st_size == 0
                or max(identity.st_mtime_ns, identity.st_ctime_ns) > verified_at):
            raise UserError(f'Final output changed since verification: {path}. Temporary files kept.')
        outputs.append((path, identity))

    targets = [root / name for name in ('tmp', 'models', 'code', 'input') if (root / name).exists() or (root / name).is_symlink()]
    input_paths = {Path(row['path']).resolve() for row in config['probe']['files']}
    protected = set(input_paths)
    original = Path(config['original_input']).resolve() if config.get('original_input') else None
    if original:
        protected.add(original)
    staged = root / 'input'
    if staged in targets:
        if not config.get('stage_input') or original is None or original.is_relative_to(staged):
            raise UserError('Cannot confirm that input/ contains only staging copies; files kept.')
        for row in config['probe']['files']:
            copy = Path(row['path']).resolve()
            if not copy.is_relative_to(staged):
                raise UserError('Staged input manifest does not match input/; files kept.')
            source = original if original.is_file() else original / copy.relative_to(staged)
            if (not source.is_file() or source.stat().st_size != row['bytes']
                    or source.stat().st_mtime_ns != row['mtime_ns']):
                raise UserError(f'Original POD5 missing or changed: {source}. Staging copies kept.')
            protected.add(source.resolve())
        protected -= input_paths
    for target in targets:
        if target.is_symlink() or not target.is_dir():
            raise UserError(f'Cleanup folder must be a regular directory: {target}')
        if any(path == target or path.is_relative_to(target) for path in protected):
            raise UserError(f'Cleanup folder contains source input: {target}')

    files, directories = [], []
    for target in targets:
        for folder, subdirs, names in os.walk(target, followlinks=False):
            directory = Path(folder)
            directories.append(directory)
            for name in subdirs:
                if (directory / name).is_symlink():
                    raise UserError('Cleanup folder contains a symbolic link; files kept.')
            for name in names:
                path = directory / name
                identity = path.lstat()
                if (stat.S_ISLNK(identity.st_mode) and target == root / 'tmp'
                        and path.relative_to(target).parts[0] == 'shards'
                        and path.resolve() in input_paths):
                    # Per-part input links; only the link is removed, never its POD5 target.
                    files.append((path, identity))
                    continue
                if not stat.S_ISREG(identity.st_mode):
                    raise UserError(f'Cleanup file is not a regular file: {path}')
                if path.suffix.lower() == '.pod5' and target != staged:
                    raise UserError(f'Unexpected POD5 in a cleanup folder: {path}. Files kept.')
                if target == staged and path.resolve() not in input_paths:
                    raise UserError(f'File is not in the staging manifest: {path}')
                files.append((path, identity))
    for name in ('job.sh', 'job_merge.sh', 'submission.lock'):
        path = root / name
        if path.exists() or path.is_symlink():
            identity = path.lstat()
            if not stat.S_ISREG(identity.st_mode):
                raise UserError(f'Cleanup file is not a regular file: {path}')
            files.append((path, identity))

    moves = []
    for name in ('status.json', 'submission.json', 'verification.json', 'sample_counts.tsv', 'run.log'):
        source, destination = root / name, root / 'logs' / name
        if source.exists() or source.is_symlink():
            if source.is_symlink() or not source.is_file() or destination.exists() or destination.is_symlink():
                raise UserError(f'Cannot move {source} into logs/ without replacing a file.')
            moves.append((source, destination))
    for path, identity in outputs + files:
        if path.lstat() != identity:
            raise UserError(f'File changed during cleanup checks: {path}')
    if not files and not directories and not moves:
        return {'files_removed': 0, 'bytes_removed': 0}
    record = {'state': 'removing', 'started_at': timestamp(),
              'directories': [str(path.relative_to(root)) for path in targets],
              'files': [str(path.relative_to(root)) for path, _ in files],
              'files_removed': 0, 'bytes_removed': 0}
    audit = root / 'logs/cleanup.json'
    write_json(audit, record)
    try:
        for source, destination in moves:
            source.rename(destination)
        for path, identity in files:
            path.unlink()
            record['files_removed'] += 1
            record['bytes_removed'] += identity.st_size
        for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            directory.rmdir()
        record.update(state='complete', completed_at=timestamp())
    except BaseException:
        record.update(state='incomplete', updated_at=timestamp())
        raise
    finally:
        write_json(audit, record)
    return record
