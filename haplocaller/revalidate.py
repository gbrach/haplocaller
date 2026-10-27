"""Maintainer recovery of an export stopped by the old Dorado counter check.

Reads existing output only; never invokes Dorado or rewrites sequence files.
Run inside a CPU allocation: python -m haplocaller.revalidate RUN --workers 4.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .common import run_file, timestamp, write_json
from .verification import COUNT_FIELDS, dorado_counts, write_report
from .worker import fastq_records, log, state

MASK = (1 << 256) - 1


def identity_hash(identifier, length):
    # An order-independent SHA256 multiset fingerprint: duplicates contribute
    # repeatedly. Count and base totals are also compared independently.
    return int.from_bytes(hashlib.sha256(identifier + b'\0' + str(length).encode()).digest(), 'big')


def summary_counts(path, config):
    allowed = {r['alias'] for r in config['mapping']} | {'unclassified'}
    rows = {alias: dict(alias=alias, **{k: 0 for k in COUNT_FIELDS[1:]}, fingerprint=0)
            for alias in allowed}
    with path.open('rb') as stream:
        header = stream.readline().decode().rstrip('\r\n').split('\t')
        required = ('read_id', 'alias', 'passes_filtering', 'sequence_length_template')
        if not all(key in header for key in required):
            raise ValueError('Sequencing summary lacks the required verification columns.')
        columns = {key: header.index(key) for key in required}
        for line in stream:
            values = line.rstrip(b'\r\n').split(b'\t')
            if not line.endswith(b'\n') or len(values) != len(header):
                raise ValueError('Truncated or malformed sequencing summary.')
            alias = values[columns['alias']].decode()
            if alias not in rows:
                raise ValueError(f'Unexpected summary alias: {alias}')
            flag = values[columns['passes_filtering']].lower()
            if flag not in (b'true', b'false', b'1', b'0'):
                raise ValueError('Invalid summary quality-filter flag.')
            length = int(values[columns['sequence_length_template']])
            identifier = values[columns['read_id']]
            if length < 0 or not identifier or len(identifier.split()) != 1:
                raise ValueError('Invalid summary read ID or length.')
            row = rows[alias]
            row['native_records'] += 1
            if flag in (b'false', b'0'):
                row['discarded_quality'] += 1
            elif alias == 'unclassified' and config['unclassified'] != 'keep':
                row['discarded_unclassified'] += 1
            else:
                row['reads'] += 1
                row['bases'] += length
                row['fingerprint'] = (row['fingerprint'] + identity_hash(identifier, length)) & MASK
    return rows


def file_identity(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def inspect_output(path):
    path = Path(path)
    before = file_identity(path)
    reads = bases = fingerprint = 0
    payload = hashlib.sha256()
    if path.name.endswith('.fastq.gz'):
        for record, length in fastq_records(path):
            identifier = record.split(b'\n', 1)[0][1:].split()[0]
            reads += 1
            bases += length
            fingerprint = (fingerprint + identity_hash(identifier, length)) & MASK
            payload.update(record)
    else:
        import pysam
        with pysam.AlignmentFile(str(path), 'rb', check_sq=False) as stream:
            stream.check_truncation()
            for read in stream:
                if not read.is_unmapped or read.is_secondary or read.is_supplementary:
                    raise ValueError('Expected unaligned primary simplex output.')
                length = read.query_length or 0
                reads += 1
                bases += length
                fingerprint = (fingerprint + identity_hash(read.query_name.encode(), length)) & MASK
                payload.update(read.to_string().encode() + b'\n')
    if file_identity(path) != before:
        raise ValueError(f'Output changed during verification: {path}')
    return dict(path=str(path), reads=reads, bases=bases, fingerprint=fingerprint,
                file_identity=before, payload_sha256=payload.hexdigest())


def validate_outputs(root, config, rows, workers, progress=None):
    destination = root / config['output']
    suffix = '.fastq.gz' if config['output'] == 'fastq' else '.bam'
    expected = {alias + suffix for alias, row in rows.items() if row['reads']}
    actual = {p.name for p in destination.iterdir()}
    if actual != expected:
        raise ValueError(f'Output file set mismatch: missing {sorted(expected-actual)}, '
                         f'unexpected {sorted(actual-expected)}.')
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(inspect_output, destination / name): name for name in sorted(expected)}
        for future in as_completed(futures):
            result = future.result()
            alias = futures[future][:-len(suffix)]
            for key in ('reads', 'bases', 'fingerprint'):
                if result[key] != rows[alias][key]:
                    raise ValueError(f'{alias}: exported {key} differs from the sequencing summary.')
            results.append(result)
            if progress:
                progress(len(results), len(expected), alias)
    # Catch changes made while other files were being inspected.
    if {p.name for p in destination.iterdir()} != expected:
        raise ValueError('Output file set changed during validation.')
    for result in results:
        if file_identity(Path(result['path'])) != result['file_identity']:
            raise ValueError(f'Output changed during validation: {result["path"]}')
    return results


def check_manifest(config):
    source = Path(config['probe']['input'])
    actual = {source.resolve()} if source.is_file() else {p.resolve() for p in source.rglob('*.pod5')}
    if actual != {Path(row['path']).resolve() for row in config['probe']['files']}:
        raise ValueError('Input file list changed since preparation.')
    for row in config['probe']['files']:
        stat = Path(row['path']).stat()
        if (stat.st_size, stat.st_mtime_ns) != (row['bytes'], row['mtime_ns']):
            raise ValueError(f'Input changed since preparation: {row["path"]}')


def check_inactive_job(job):
    # Query the live list rather than -j: expired IDs make squeue return an
    # error, and some compute nodes cannot reach the sacct database at all.
    response = subprocess.run(['squeue', '-h', '-o', '%i'],
                              text=True, capture_output=True, timeout=15)
    if response.returncode == 0:
        active = {line.strip().split('_', 1)[0] for line in response.stdout.splitlines()}
        if job in active:
            raise ValueError('Original Slurm job is still active; refusing recovery.')
        return
    # If the live query fails, require explicit terminal accounting evidence.
    history = subprocess.run(['sacct', '-X', '-j', job, '-nP', '--format=JobIDRaw,State'],
                             text=True, capture_output=True, check=True, timeout=15)
    states = [line.split('|')[1].split()[0].rstrip('+')
              for line in history.stdout.splitlines()
              if line.split('|')[0].strip() == job and len(line.split('|')) >= 2]
    terminal = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY',
                'NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'REVOKED'}
    if len(states) != 1 or states[0] not in terminal:
        raise ValueError('Cannot confirm the original Slurm job has ended; refusing recovery.')


def recover(root, workers):
    config = json.loads((root / 'config/run.json').read_text())
    previous = json.loads((run_file(root, 'status.json')).read_text())
    submission = json.loads((run_file(root, 'submission.json')).read_text())
    original_job = str(submission['job_id'])
    # This path deliberately handles only the known post-export counter bug.
    message = previous.get('message', '')
    if (previous.get('state') != 'failed' or not message.startswith('Stopped during exporting:')
            or not any(text in message for text in ('raw reads; Dorado reports', 'Dorado demultiplexed count'))):
        raise ValueError('Recovery requires the known post-export counter-check failure.')
    if not original_job.isdigit():
        raise ValueError('Invalid original Slurm job ID.')
    check_inactive_job(original_job)
    text = (root / 'logs/basecaller.log').read_text()
    if (not re.search(r'> Finished\s*$', text)
            or 'Basecalling finished. Creating and checking one file per sample.' not in (run_file(root, 'run.log')).read_text()):
        raise ValueError('No evidence that Dorado exited successfully before export.')
    check_manifest(config)
    audit = root / 'logs' / ('revalidation-' + timestamp().replace(':', '').replace('+', '_'))
    audit.mkdir()
    write_json(audit / 'previous_status.json', previous)
    shutil.copy2(run_file(root, 'submission.json'), audit)
    shutil.copytree(Path(__file__).parent, audit / 'code/haplocaller',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    summary = root / 'tmp/native/sequencing_summary.txt'
    summary_before = file_identity(summary)
    try:
        state(root, 'verifying', 'Revalidating existing output after the counter-check failure; no basecalling.')
        rows = summary_counts(summary, config)
        expected = config['probe']['reads']
        if config['mode'] == 'pilot':
            expected = min(1000, expected)
        counters = dorado_counts(text, expected, sum(r['native_records'] for r in rows.values()))
        write_json(audit / 'summary_counts.json', rows)
        log(root, f'Summary accounting matches: {counters["basecall_records"]:,} written records + '
                  f'{counters["dorado_filtered_records"]:,} internally filtered records. Checking output files.')
        results = validate_outputs(root, config, rows, workers,
                    lambda done, total, alias: log(root, f'Revalidated {done}/{total} files: {alias}.'))
        if file_identity(summary) != summary_before:
            raise ValueError('Sequencing summary changed during validation.')
        check_manifest(config)
        write_json(audit / 'output_checks.json', results)
        recovery = {'method': 'existing_outputs', 'original_job_id': original_job,
                    'validation_job_id': os.environ.get('SLURM_JOB_ID'),
                    'completed_at': timestamp(), 'audit': str(audit),
                    'files_checked': len(results)}
        report = write_report(root, config, list(rows.values()), counters, recovery=recovery)
        state(root, 'pilot_complete' if config['mode'] == 'pilot' else 'complete',
              f'Complete after revalidation: {report["kept_reads"]:,} retained records in {len(results)} files. '
              f'Original Slurm job {original_job} retains its historical export-check failure.')
        return report
    except BaseException as error:
        write_json(audit / 'error.json', {'error': str(error), 'at': timestamp()})
        write_json(run_file(root, 'status.json'), previous)
        log(root, f'Revalidation failed: {error}. Original failure status retained.')
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be positive')
    if not os.environ.get('SLURM_JOB_ID'):
        parser.error('Run revalidation inside a CPU Slurm allocation.')
    recover(args.run.resolve(strict=True), args.workers)


if __name__ == '__main__':
    main()
