"""Snapshot this module into each run; execute only inside its Slurm allocation."""
import gzip
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import run_file, UserError, human_size, qscore_integer, timestamp, write_json
from .verification import dorado_counts, write_report


def log(root, message):
    line = timestamp() + '  ' + message
    print(line, flush=True)
    with (run_file(root, 'run.log')).open('a') as stream:
        stream.write(line + '\n')


def state(root, name, message):
    write_json(run_file(root, 'status.json'), {'state': name, 'message': message, 'updated_at': timestamp()})
    log(root, message)


def fastq_records(path):
    opener = gzip.open if path.name.endswith('.gz') else open
    with opener(path, 'rb') as stream:
        while True:
            header = stream.readline()
            if not header:
                break
            sequence = stream.readline()
            separator = stream.readline()
            quality = stream.readline()
            if (not header.startswith(b'@') or not separator.startswith(b'+')
                    or not quality.endswith(b'\n')
                    or len(sequence.rstrip(b'\r\n')) != len(quality.rstrip(b'\r\n'))):
                raise ValueError(f'Invalid FASTQ record in {path}')
            yield header + sequence + separator + quality, len(sequence.rstrip(b'\r\n'))


def native_groups(native, config):
    groups = defaultdict(list)
    prefix = 'fastq' if config['output'] == 'fastq' else 'bam'
    allowed = {row['alias'] for row in config['mapping']} | {'unclassified'}
    for path in sorted(native.rglob('*')):
        if not path.is_file():
            continue
        if prefix == 'fastq' and not path.name.endswith(('.fastq', '.fastq.gz')):
            continue
        if prefix == 'bam' and path.suffix != '.bam':
            continue
        alias = path.parent.name
        if alias not in allowed:
            raise ValueError(f'Unexpected sample "{alias}"; check the barcode table and run metadata.')
        if prefix + '_pass' in path.parts:
            quality_pass = True
        elif prefix + '_fail' in path.parts:
            quality_pass = False
        else:
            raise ValueError(f'Unrecognised Dorado output folder: {path}')
        groups[alias].append((path, quality_pass))
    if not groups:
        raise ValueError('Dorado produced no recognised basecall files.')
    return groups


def merge_header(paths):
    import pysam
    header, records = {'HD': {'VN': '1.6', 'SO': 'unknown'}, 'SQ': []}, {'RG': {}, 'PG': {}}
    for path in paths:
        with pysam.AlignmentFile(str(path), 'rb', check_sq=False) as stream:
            item = stream.header.to_dict()
            if item.get('SQ'):
                raise ValueError('This workflow expects unaligned Dorado BAMs.')
            for key in ('RG', 'PG'):
                for value in item.get(key, []):
                    identity = value['ID']
                    if identity in records[key] and records[key][identity] != value:
                        raise ValueError(f'Conflicting BAM {key} header: {identity}')
                    records[key][identity] = value
    for key, values in records.items():
        if values:
            header[key] = list(values.values())
    return header


def export_alias(item, destination, config):
    alias, sources = item
    result = {'alias': alias, 'reads': 0, 'bases': 0, 'discarded_quality': 0,
              'discarded_unclassified': 0, 'native_records': 0}
    keep_unclassified = alias != 'unclassified' or config['unclassified'] == 'keep'
    kept_sources = [path for path, passed in sources if passed and keep_unclassified]
    is_fastq = config['output'] == 'fastq'
    output = destination / (alias + ('.fastq.gz' if is_fastq else '.bam'))
    partial = output.with_name(output.name + '.partial')
    writer = None
    raw_output = None
    try:
        if kept_sources:
            if is_fastq:
                raw_output = partial.open('xb')
                writer = gzip.GzipFile(filename='', fileobj=raw_output, mode='wb', compresslevel=1, mtime=0)
            else:
                import pysam
                writer = pysam.AlignmentFile(str(partial), 'wb', header=merge_header(kept_sources))
        for path, passed in sources:
            if is_fastq:
                stream = fastq_records(path)
                closer = None
            else:
                import pysam
                closer = pysam.AlignmentFile(str(path), 'rb', check_sq=False)
                stream = ((read, read.query_length or 0) for read in closer)
            try:
                for read, length in stream:
                    result['native_records'] += 1
                    if result['native_records'] % 1000 == 0 and shutil.disk_usage(destination).free < config['profile'].get('minimum_free_gib', 5) * 1024**3:
                        raise RuntimeError('Workspace is nearly full; stopping export before exhausting the disk.')
                    if not passed:
                        result['discarded_quality'] += 1
                    elif not keep_unclassified:
                        result['discarded_unclassified'] += 1
                    else:
                        writer.write(read)
                        result['reads'] += 1
                        result['bases'] += length
            finally:
                if closer is not None:
                    closer.close()
        if writer is not None:
            writer.close()
            writer = None
            if raw_output:
                raw_output.close()
                raw_output = None
            # Check the complete output before making the final name visible.
            if is_fastq:
                # Gzip detection uses the suffix; partial names need an explicit reader.
                verified = 0
                with gzip.open(partial, 'rb') as stream:
                    while True:
                        header = stream.readline()
                        if not header:
                            break
                        sequence, plus, quality = stream.readline(), stream.readline(), stream.readline()
                        if (not header.startswith(b'@') or not plus.startswith(b'+') or not quality.endswith(b'\n')
                                or len(sequence.rstrip(b'\r\n')) != len(quality.rstrip(b'\r\n'))):
                            raise ValueError(f'Invalid exported FASTQ for {alias}')
                        verified += 1
            else:
                import pysam
                with pysam.AlignmentFile(str(partial), 'rb', check_sq=False) as stream:
                    stream.check_truncation()
                    verified = sum(1 for _ in stream)
            if verified != result['reads']:
                raise ValueError(f'Exported read count mismatch for {alias}')
            partial.rename(output)
    finally:
        if writer is not None:
            writer.close()
        if raw_output is not None:
            raw_output.close()
    return result


def export_results(root, config, native, basecaller_log, expected_raw):
    groups = native_groups(native, config)
    destination = root / ('fastq' if config['output'] == 'fastq' else 'bam')
    destination.mkdir(exist_ok=False)
    workers = min(8, int(config['profile']['cpus']))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda item: export_alias(item, destination, config), groups.items()))
    # Preserve completed per-sample verification even if global accounting fails.
    (root / 'logs').mkdir(exist_ok=True)
    write_json(root / 'logs/export_counts.json', rows)
    total = sum(row['native_records'] for row in rows)
    counters = dorado_counts(basecaller_log.read_text(), expected_raw, total)
    return write_report(root, config, rows, counters)


def basecaller_command(config, root, source, native):
    args = [config['software']['executable'], 'basecaller', config['model_path'], str(source),
            '--recursive', '--device', 'cuda:all', '--kit-name', config['kit'],
            '--sample-sheet', str(root / 'config/sample_sheet.csv'),
            '--min-qscore', str(qscore_integer(config['min_qscore'])),
            '--output-dir', str(native), '--emit-summary']
    if config['output'] == 'fastq':
        args.append('--emit-fastq')
    expected = config['probe']['reads']
    if config['mode'] == 'pilot':
        expected = min(1000, expected)
        args += ['--max-reads', str(expected), '--batchsize', '64']
    else:
        args += ['--batchsize', '0']
    return args, expected


def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    config = json.loads((root / 'config/run.json').read_text())
    process = None
    phase = 'preflight'

    def interrupted(signum, frame):
        raise InterruptedError('Job interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        state(root, 'checking', 'Checking inputs and the GPU allocation.')
        if not os.environ.get('SLURM_JOB_ID'):
            raise RuntimeError('The processing backend must run inside Slurm. Use haplocaller to submit it.')
        visible = os.environ.get('CUDA_VISIBLE_DEVICES', '').split(',')
        if (len(visible) != config['gpus'] or len(set(visible)) != len(visible)
                or any(not value or value == '-1' for value in visible)):
            raise RuntimeError(f'Expected {config["gpus"]} visible GPUs, found {visible}.')
        for row in config['probe']['files']:
            stat = Path(row['path']).stat()
            if stat.st_size != row['bytes'] or stat.st_mtime_ns != row['mtime_ns']:
                raise RuntimeError(f'Input changed after preparation: {row["path"]}')
        source = Path(config['probe']['input'])
        current_files = {source.resolve()} if source.is_file() else {p.resolve() for p in source.rglob('*.pod5')}
        if current_files != {Path(row['path']) for row in config['probe']['files']}:
            raise RuntimeError('The input file list changed after preparation. Start a new preparation.')
        for directory in ('tmp/cache', 'tmp/cuda'):
            (root / directory).mkdir(parents=True, exist_ok=True)
        version = subprocess.run([config['software']['executable'], '--version'], capture_output=True, text=True, check=True)
        (root / 'logs/dorado-version.txt').write_text(version.stdout + version.stderr)
        if config['software']['version'] not in version.stdout + version.stderr:
            raise RuntimeError('The Dorado version changed after preparation. Prepare a new run.')
        native = root / 'tmp/native'
        if native.exists() or (root / 'fastq').exists() or (root / 'bam').exists():
            raise RuntimeError('This run already has output. Use a new run name to preserve it.')
        with (root / 'logs/gpus.txt').open('w') as stream:
            subprocess.run(['nvidia-smi'], stdout=stream, stderr=subprocess.STDOUT, check=True)
        write_json(root / 'logs/allocation.json', {'job_id': os.environ['SLURM_JOB_ID'],
                   'cuda_visible_devices': visible, 'device': 'cuda:all'})
        args, expected = basecaller_command(config, root, source, native)
        write_json(root / 'logs/command.json', args)
        reserve = config['profile'].get('minimum_free_gib', 5) * 1024**3
        if shutil.disk_usage(root).free < reserve:
            raise RuntimeError('Not enough free workspace for this job.')
        phase = 'basecalling'
        state(root, phase, f'{"Small test" if config["mode"] == "pilot" else "Full run"}: {expected:,} raw reads, '
              f'{config["tier"].upper()}, {config["gpus"]} GPU(s).')
        started = time.monotonic()
        last_progress = started
        with (root / 'logs/basecaller.log').open('w') as stderr, (root / 'logs/basecaller.stdout.log').open('w') as stdout:
            process = subprocess.Popen(args, stdout=stdout, stderr=stderr)
            while True:
                try:
                    code = process.wait(timeout=15)
                    break
                except subprocess.TimeoutExpired:
                    if shutil.disk_usage(root).free < reserve:
                        process.terminate()
                        raise RuntimeError('Workspace is nearly full; stopped before exhausting the disk.')
                    if time.monotonic() - last_progress >= 300:
                        count = sum(1 for p in native.rglob('*') if p.is_file())
                        log(root, f'Basecalling continues ({int((time.monotonic()-started)/60)} min; {count} output files so far).')
                        last_progress = time.monotonic()
        process = None
        if code:
            raise RuntimeError(f'Dorado exited with code {code}. See logs/basecaller.log.')
        phase = 'exporting'
        state(root, phase, 'Basecalling finished. Creating and checking one file per sample.')
        report = export_results(root, config, native, root / 'logs/basecaller.log', expected)
        phase = 'complete'
        state(root, 'pilot_complete' if config['mode'] == 'pilot' else 'complete',
              f'Finished: {report["kept_reads"]:,} retained records. Results: {root/config["output"]}; counts: logs/sample_counts.tsv.')
        from .cleanup import cleanup_run
        try:
            cleaned = cleanup_run(root, check_scheduler=False)
            log(root, f'Cleanup: {cleaned["files_removed"]} files, '
                      f'{human_size(cleaned["bytes_removed"])}.')
        except (OSError, ValueError, UserError) as error:
            log(root, f'Cleanup failed: {error}. Retry with haplocaller clean {root}')
    except BaseException as error:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        state(root, 'interrupted' if isinstance(error, (KeyboardInterrupt, InterruptedError)) else 'failed',
              f'Stopped during {phase}: {error}. Existing outputs are kept; no automatic restart.')
        raise


if __name__ == '__main__':
    main()
