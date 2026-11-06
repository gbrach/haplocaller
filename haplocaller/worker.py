"""Snapshot this module into each run; execute only inside its Slurm allocation."""
import gzip
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import native_format, outputs, run_file, UserError, human_size, qscore_integer, timestamp, write_json
from .verification import combine_counters, dorado_counts, write_report


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
    prefix = native_format(config)
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


SUFFIX = {'fastq': '.fastq.gz', 'bam': '.bam'}


ARRAY_TYPE = {'b': 'c', 'B': 'C', 'h': 's', 'H': 'S', 'i': 'i', 'I': 'I', 'f': 'f'}


def sam_tag(tag, value, kind):
    if kind == 'B':
        return f'{tag}:B:{ARRAY_TYPE[value.typecode]},' + ','.join(f'{x:g}' if value.typecode == 'f' else str(x) for x in value)
    if kind == 'f':
        return f'{tag}:f:{value:g}'
    kind = 'i' if kind in 'cCsSiI' else kind
    return f'{tag}:{kind}:{value}'


FASTQ_TAGS = ('qs', 'ch', 'st', 'RG')


def fastq_text(read):
    """FASTQ record made from a BAM record. Dorado's own FASTQ also lists the GPU and trim tags, which the BAM
    does not hold; the four tags written here have exactly the same values as in Dorado's FASTQ."""
    tags = [sam_tag(tag, value, kind) for tag, value, kind in read.get_tags(with_value_type=True) if tag in FASTQ_TAGS]
    tags.sort(key=lambda text: FASTQ_TAGS.index(text[:2]))
    header = '\t'.join(['@' + read.query_name] + tags)
    return f'{header}\n{read.query_sequence or ""}\n+\n{read.qual or ""}\n'.encode()


def parent_id(read):
    return read.get_tag('pi') if read.has_tag('pi') else read.query_name


def check_fastq(path, alias):
    verified = 0
    with gzip.open(path, 'rb') as stream:  # partial names need an explicit gzip reader
        while True:
            header = stream.readline()
            if not header:
                break
            sequence, plus, quality = stream.readline(), stream.readline(), stream.readline()
            if (not header.startswith(b'@') or not plus.startswith(b'+') or not quality.endswith(b'\n')
                    or len(sequence.rstrip(b'\r\n')) != len(quality.rstrip(b'\r\n'))):
                raise ValueError(f'Invalid exported FASTQ for {alias}')
            verified += 1
    return verified


def export_alias(item, root, config):
    """One sample: write every requested format from a single pass over its native records.
    Returns (counts, parent raw-read IDs seen for this sample)."""
    alias, sources = item
    kinds, native = outputs(config), native_format(config)
    result = {'alias': alias, 'reads': 0, 'bases': 0, 'discarded_quality': 0,
              'discarded_unclassified': 0, 'native_records': 0}
    parents = set()
    if config.get('shards'):
        result['by_source'] = {}
    keep_unclassified = alias != 'unclassified' or config['unclassified'] == 'keep'
    kept_sources = [path for path, passed in sources if passed and keep_unclassified]
    finals = {kind: root / kind / (alias + SUFFIX[kind]) for kind in kinds}
    partials = {kind: path.with_name(path.name + '.partial') for kind, path in finals.items()}
    writers, raw_files = {}, {}
    try:
        if kept_sources:
            for kind in kinds:
                if kind == 'fastq':
                    raw_files[kind] = partials[kind].open('xb')
                    writers[kind] = gzip.GzipFile(filename='', fileobj=raw_files[kind], mode='wb', compresslevel=1, mtime=0)
                else:
                    import pysam
                    writers[kind] = pysam.AlignmentFile(str(partials[kind]), 'wb', header=merge_header(kept_sources))
        for path, passed in sources:
            if native == 'fastq':
                stream = fastq_records(path)
                closer = None
            else:
                import pysam
                closer = pysam.AlignmentFile(str(path), 'rb', check_sq=False)
                stream = ((read, read.query_length or 0) for read in closer)
            try:
                for read, length in stream:
                    result['native_records'] += 1
                    if config.get('pod5'):
                        parents.add(uuid.UUID(parent_id(read)).bytes)
                    if 'by_source' in result:
                        source_key = path.relative_to(root / 'tmp').parts[0]
                        result['by_source'][source_key] = result['by_source'].get(source_key, 0) + 1
                    if result['native_records'] % 1000 == 0 and shutil.disk_usage(root).free < config['profile'].get('minimum_free_gib', 5) * 1024**3:
                        raise RuntimeError('Workspace is nearly full; stopping export before exhausting the disk.')
                    if not passed:
                        result['discarded_quality'] += 1
                    elif not keep_unclassified:
                        result['discarded_unclassified'] += 1
                    else:
                        for kind, writer in writers.items():
                            writer.write(fastq_text(read) if kind == 'fastq' and native == 'bam' else read)
                        result['reads'] += 1
                        result['bases'] += length
            finally:
                if closer is not None:
                    closer.close()
        if writers:
            for kind in list(writers):
                writers.pop(kind).close()
                if kind in raw_files:
                    raw_files.pop(kind).close()
            # Check every complete output before any final name becomes visible.
            for kind in kinds:
                if kind == 'fastq':
                    verified = check_fastq(partials[kind], alias)
                else:
                    import pysam
                    with pysam.AlignmentFile(str(partials[kind]), 'rb', check_sq=False) as stream:
                        stream.check_truncation()
                        verified = sum(1 for _ in stream)
                if verified != result['reads']:
                    raise ValueError(f'Exported read count mismatch for {alias} ({kind})')
            for kind in kinds:
                partials[kind].rename(finals[kind])
    finally:
        for writer in writers.values():
            writer.close()
        for stream in raw_files.values():
            stream.close()
    return result, parents


def finished_message(root, config, report):
    text = (f'Finished: {report["kept_reads"]:,} retained records. Results: '
            f'{", ".join(str(root / kind) for kind in outputs(config))}; counts: logs/sample_counts.tsv.')
    pod5 = report.get('pod5')
    if pod5 and pod5.get('status') == 'passed':
        text += f' Demultiplexed POD5: {pod5["reads"]:,} raw reads in {root / "pod5"}.'
    elif pod5:
        text += f' POD5 EXPORT FAILED ({pod5.get("error")}); partial files removed. The basecalls above are complete.'
    return text


def export_results(root, config, native, basecaller_log, expected_raw):
    return export_parts(root, config, [(native, basecaller_log, expected_raw)])


def export_parts(root, config, parts):
    """parts: [(native_dir, dorado_log, expected_raw_reads)]; one part for an unsharded run."""
    groups = defaultdict(list)
    for native, _, _ in parts:
        for alias, sources in native_groups(native, config).items():
            groups[alias].extend(sources)
    for kind in outputs(config):
        (root / kind).mkdir(exist_ok=False)
    workers = min(8, int(config['profile']['cpus']))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        exported = list(pool.map(lambda item: export_alias(item, root, config), groups.items()))
    rows = [row for row, _ in exported]
    # Preserve completed per-sample verification even if global accounting fails.
    (root / 'logs').mkdir(exist_ok=True)
    write_json(root / 'logs/export_counts.json', rows)
    total = sum(row['native_records'] for row in rows)
    if len(parts) == 1:
        counters = dorado_counts(parts[0][1].read_text(), parts[0][2], total)
    else:
        counters = []
        for native, log_path, expected in parts:
            written = sum(row['by_source'].get(native.relative_to(root / 'tmp').parts[0], 0) for row in rows)
            counters.append(dorado_counts(log_path.read_text(), expected, written))
        counters = combine_counters(counters)
    report = write_report(root, config, rows, counters)
    if config.get('pod5'):
        # Last, so that a full disk here cannot cost the finished, verified basecalls.
        routes = {row['alias']: found for row, found in exported}
        state(root, 'exporting', 'Basecalls are final and verified. Now writing the demultiplexed POD5; this can take hours.')
        try:
            report['pod5'] = export_pod5(root, config, routes)
            log(root, f'POD5 export: {report["pod5"]["reads"]:,} raw reads in {report["pod5"]["files"]} files.')
        except Exception as error:
            shutil.rmtree(root / 'pod5', ignore_errors=True)
            report['pod5'] = {'status': 'failed', 'error': str(error)}
            log(root, f'POD5 export FAILED, partial files removed; the basecalls are complete and verified: {error}')
        except BaseException:
            shutil.rmtree(root / 'pod5', ignore_errors=True)
            raise
        write_json(run_file(root, 'verification.json'), report)
    return report


def raw_signature(record):
    """A 16-byte digest of the raw signal and read metadata; compared before and after copying."""
    import hashlib
    digest = hashlib.sha256(record.signal.tobytes())
    for value in (record.num_samples, record.read_number, record.start_sample, record.pore,
                  record.calibration, record.end_reason, record.median_before, record.run_info):
        digest.update(repr(value).encode())
    return int.from_bytes(digest.digest()[:16], 'big')


def export_pod5(root, config, routes):
    """Write one POD5 per sample holding the raw signal of every read assigned to it, whatever its Q score.
    Reads with calls in two samples go to _ambiguous.pod5, reads without any call to _uncalled.pod5.
    A small test only archives the reads it actually basecalled."""
    import pod5
    destinations = {}
    for alias, found in routes.items():
        for parent in found:
            previous = destinations.setdefault(parent, alias)
            if previous != alias:
                destinations[parent] = '_ambiguous'
    full = config['mode'] != 'pilot'
    folder = root / 'pod5'
    folder.mkdir(exist_ok=False)
    reserve = config['profile'].get('minimum_free_gib', 5) * 1024**3
    written = {}
    writers = {}
    from contextlib import ExitStack
    with ExitStack() as stack:
        seen = done_files = 0
        for row in config['probe']['files']:
            with pod5.Reader(Path(row['path'])) as reader:
                for record in reader.reads():
                    if not full and seen >= len(destinations):
                        break  # a small test has everything it needs
                    key = record.read_id.bytes
                    name = destinations.get(key, '_uncalled' if full else None)
                    if name is None:
                        continue
                    if name not in writers:
                        writers[name] = stack.enter_context(pod5.Writer(folder / (name + '.pod5.partial')))
                    writers[name].add_read(record.to_read())
                    count, total = written.get(name, (0, 0))
                    written[name] = (count + 1, (total + raw_signature(record)) % (1 << 128))
                    seen += 1
                    if seen % 1000 == 0 and shutil.disk_usage(root).free < reserve:
                        raise RuntimeError('Workspace is nearly full; stopped the POD5 export before exhausting the disk.')
            done_files += 1
            log(root, f'POD5 export: input file {done_files}/{len(config["probe"]["files"])} done, {seen:,} raw reads written.')
            if not full and seen >= len(destinations):
                break
    log(root, 'POD5 export: checking the written files against the input signals.')
    # Reopen everything that was written and compare it with what was meant to be written.
    for name, (count, total) in written.items():
        check_count, check_total = 0, 0
        with pod5.Reader(folder / (name + '.pod5.partial')) as reader:
            for record in reader.reads():
                expected = destinations.get(record.read_id.bytes, '_uncalled')
                if expected != name:
                    raise ValueError(f'Raw read in the wrong POD5 file: {name}')
                check_count += 1
                check_total = (check_total + raw_signature(record)) % (1 << 128)
        if (check_count, check_total) != (count, total):
            raise ValueError(f'POD5 file {name}.pod5 does not match the input signals.')
    for name in written:
        (folder / (name + '.pod5.partial')).rename(folder / (name + '.pod5'))
    reads = sum(count for count, _ in written.values())
    if full and reads != config['probe']['reads']:
        raise ValueError(f'POD5 export holds {reads:,} reads but the input has {config["probe"]["reads"]:,}.')
    return {'status': 'passed', 'files': len(written), 'reads': reads,
            'bytes': sum(p.stat().st_size for p in folder.glob('*.pod5')),
            'uncalled': written.get('_uncalled', (0, 0))[0], 'ambiguous': written.get('_ambiguous', (0, 0))[0],
            'policy': 'all reads, including those below the Q-score threshold'}


def basecaller_command(config, root, source, native):
    args = [config['software']['executable'], 'basecaller', config['model_path'], str(source),
            '--recursive', '--device', 'cuda:all', '--kit-name', config['kit'],
            '--sample-sheet', str(root / 'config/sample_sheet.csv'),
            '--min-qscore', str(qscore_integer(config['min_qscore'])),
            '--output-dir', str(native), '--emit-summary']
    if native_format(config) == 'fastq':
        args.append('--emit-fastq')
    expected = config['probe']['reads']
    if config['mode'] == 'pilot':
        expected = min(1000, expected)
        args += ['--max-reads', str(expected), '--batchsize', '64']
    else:
        args += ['--batchsize', '0']
    return args, expected


def preflight(root, config, files=None):
    """Check GPUs, unchanged inputs and the Dorado version. files: this part's paths, or all."""
    wanted = set(files) if files is not None else None
    rows = [row for row in config['probe']['files'] if wanted is None or row['path'] in wanted]
    visible = os.environ.get('CUDA_VISIBLE_DEVICES', '').split(',')
    if (len(visible) != config['gpus'] or len(set(visible)) != len(visible)
            or any(not value or value == '-1' for value in visible)):
        raise RuntimeError(f'Expected {config["gpus"]} visible GPUs, found {visible}.')
    for row in rows:
        stat = Path(row['path']).stat()
        if stat.st_size != row['bytes'] or stat.st_mtime_ns != row['mtime_ns']:
            raise RuntimeError(f'Input changed after preparation: {row["path"]}')
    if wanted is None:
        source = Path(config['probe']['input'])
        current_files = {source.resolve()} if source.is_file() else {p.resolve() for p in source.rglob('*.pod5')}
        if current_files != {Path(row['path']) for row in config['probe']['files']}:
            raise RuntimeError('The input file list changed after preparation. Start a new preparation.')
    for directory in ('tmp/cache', 'tmp/cuda'):
        (root / directory).mkdir(parents=True, exist_ok=True)
    version = subprocess.run([config['software']['executable'], '--version'], capture_output=True, text=True, check=True)
    if config['software']['version'] not in version.stdout + version.stderr:
        raise RuntimeError('The Dorado version changed after preparation. Prepare a new run.')
    return visible, version.stdout + version.stderr


def shard_main(root, config, index):
    """One Dorado process for one part of a sharded run (a Slurm array task)."""
    files = config['shards'][index]
    label = f'part {index + 1}/{len(config["shards"])}'
    expected = sum(row['reads'] for row in config['probe']['files'] if row['path'] in set(files))
    process = None

    def interrupted(signum, frame):
        raise InterruptedError('Job interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        visible, version_text = preflight(root, config, files)
        (root / 'logs').mkdir(exist_ok=True)
        (root / f'logs/dorado-version.txt').write_text(version_text)
        native = root / f'tmp/native_{index}'
        if native.exists() or (root / 'fastq').exists() or (root / 'bam').exists():
            raise RuntimeError('This part already has output. Use a new run name to preserve it.')
        inputs = root / f'tmp/shards/{index}'
        inputs.mkdir(parents=True)
        for number, path in enumerate(files):
            (inputs / f'{number:05d}_{Path(path).name}').symlink_to(path)
        with (root / f'logs/gpus_{index}.txt').open('w') as stream:
            subprocess.run(['nvidia-smi'], stdout=stream, stderr=subprocess.STDOUT, check=True)
        write_json(root / f'logs/allocation_{index}.json', {'job_id': os.environ.get('SLURM_JOB_ID'),
                   'array_task': index, 'cuda_visible_devices': visible, 'device': 'cuda:all'})
        args, _ = basecaller_command(config, root, inputs, native)
        write_json(root / f'logs/command_{index}.json', args)
        reserve = config['profile'].get('minimum_free_gib', 5) * 1024**3
        state(root, 'basecalling', f'Full run, {label}: {expected:,} raw reads in {len(files)} POD5 files, '
              f'{config["tier"].upper()}, {config["gpus"]} GPU(s) per part.')
        with (root / f'logs/basecaller_{index}.log').open('w') as stderr, \
                (root / f'logs/basecaller_{index}.stdout.log').open('w') as stdout:
            process = subprocess.Popen(args, stdout=stdout, stderr=stderr)
            while True:
                try:
                    code = process.wait(timeout=15)
                    break
                except subprocess.TimeoutExpired:
                    if shutil.disk_usage(root).free < reserve:
                        process.terminate()
                        raise RuntimeError('Workspace is nearly full; stopped before exhausting the disk.')
        process = None
        if code:
            raise RuntimeError(f'Dorado exited with code {code}. See logs/basecaller_{index}.log.')
        write_json(root / f'tmp/shard_{index}.done', {'expected_raw': expected, 'completed_at': timestamp()})
        log(root, f'Basecalling {label} finished.')
    except BaseException as error:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        state(root, 'interrupted' if isinstance(error, (KeyboardInterrupt, InterruptedError)) else 'failed',
              f'Stopped during basecalling {label}: {error}. Other parts may still be running; '
              'cancel them with haplocaller cancel. Existing outputs are kept; no automatic restart.')
        raise


def merge_main(root, config):
    """CPU job that runs after every part has succeeded: export, verify and clean up."""
    count = len(config['shards'])
    try:
        parts = []
        for index in range(count):
            done = root / f'tmp/shard_{index}.done'
            if not done.is_file():
                raise RuntimeError(f'Part {index + 1}/{count} did not finish; nothing to merge.')
            parts.append((root / f'tmp/native_{index}', root / f'logs/basecaller_{index}.log',
                          json.loads(done.read_text())['expected_raw']))
        if sum(part[2] for part in parts) != config['probe']['reads']:
            raise RuntimeError('Part read counts do not add up to the input manifest.')
        if (root / 'fastq').exists() or (root / 'bam').exists():
            raise RuntimeError('This run already has output. Use a new run name to preserve it.')
        state(root, 'exporting', f'All {count} parts finished. Creating and checking one file per sample.')
        report = export_parts(root, config, parts)
        state(root, 'complete', finished_message(root, config, report))
        from .cleanup import cleanup_run
        try:
            cleaned = cleanup_run(root, check_scheduler=False)
            log(root, f'Cleanup: {cleaned["files_removed"]} files, {human_size(cleaned["bytes_removed"])}.')
        except (OSError, ValueError, UserError) as error:
            log(root, f'Cleanup failed: {error}. Retry with haplocaller clean {root}')
    except BaseException as error:
        state(root, 'failed', f'Stopped during merge: {error}. Existing outputs are kept; no automatic restart.')
        raise


def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    config = json.loads((root / 'config/run.json').read_text())
    role = sys.argv[2] if len(sys.argv) > 2 else ('shard' if os.environ.get('SLURM_ARRAY_TASK_ID') else 'single')
    if role == 'shard':
        return shard_main(root, config, int(os.environ['SLURM_ARRAY_TASK_ID']))
    if role == 'merge':
        return merge_main(root, config)
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
        visible, version_text = preflight(root, config)
        (root / 'logs/dorado-version.txt').write_text(version_text)
        source = Path(config['probe']['input'])
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
        state(root, 'pilot_complete' if config['mode'] == 'pilot' else 'complete', finished_message(root, config, report))
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
