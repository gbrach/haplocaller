"""Read-only job monitoring with an incremental, disk-backed summary index.

The cache is separate from run files. Distinct parent UUIDs prevent split reads
from inflating raw-read progress. Only complete summary lines are committed.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import ui
from .common import run_file, UserError, human_size
from .verification import revalidated

BAD_STATES = {'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL',
              'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'REVOKED'}
FINISHED = {'complete', 'pilot_complete'}
STOPPED = {'failed', 'interrupted', 'submission_failed', 'preparation_failed', 'incomplete'}


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {} if default is None else default


def tail(path, limit=4096):
    try:
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - limit))
            return stream.read().decode('utf-8', errors='replace')
    except FileNotFoundError:
        return ''


def duration(seconds):
    if seconds is None:
        return '—'
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f'{days}d {hours:02}h {minutes:02}m'
    if hours:
        return f'{hours}h {minutes:02}m {seconds:02}s'
    return f'{minutes}m {seconds:02}s'


def slurm_seconds(value):
    try:
        days, clock = value.split('-', 1) if '-' in value else ('0', value)
        pieces = [int(p) for p in clock.split(':')]
        result = 0
        for piece in pieces:
            result = result * 60 + piece
        return int(days) * 86400 + result
    except (TypeError, ValueError):
        return None


class Scheduler:
    def __init__(self, job, merge=None):
        self.job = str(job) if job else None
        self.merge = str(merge) if merge else None
        self.last_check = -float('inf')
        self.value = {}

    def get(self):
        if not self.job or time.monotonic() - self.last_check < 10:
            return self.value
        self.last_check = time.monotonic()
        if not self.job.isdigit() or (self.merge and not self.merge.isdigit()):
            return {'error': 'Invalid job ID in the submission record.'}
        if self.merge:
            return self.get_sharded()
        try:
            if shutil.which('squeue'):
                response = subprocess.run(['squeue', '-h', '-j', self.job, '-o', '%T|%M|%N|%R'],
                                          capture_output=True, text=True, timeout=5)
                lines = response.stdout.strip().splitlines()
                if lines:
                    state, elapsed, node, reason = lines[0].split('|', 3)
                    self.value = {'state': state, 'elapsed': slurm_seconds(elapsed), 'node': node, 'reason': reason}
                    return self.value
            if shutil.which('sacct'):
                response = subprocess.run(['sacct', '-X', '-j', self.job, '-nP',
                                           '--format=State,ExitCode,ElapsedRaw,NodeList'],
                                          capture_output=True, text=True, timeout=5)
                lines = [line for line in response.stdout.strip().splitlines() if line.strip()]
                if response.returncode == 0 and lines:
                    state, code, elapsed, node = lines[0].split('|')[:4]
                    self.value = {'state': state.split()[0].rstrip('+'), 'exit_code': code,
                                  'elapsed': int(elapsed), 'node': node}
                    return self.value
            self.value = {'error': 'Scheduler state unavailable; showing saved run state.'}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.value = {'error': 'Scheduler query unavailable; showing saved run state.'}
        return self.value


    def get_sharded(self):
        """Combine the array of parts and the merge job into one state."""
        jobs = self.job + ',' + self.merge
        try:
            rows = []
            if shutil.which('squeue'):
                response = subprocess.run(['squeue', '-h', '-r', '-j', jobs, '-o', '%i|%T|%M|%N|%R'],
                                          capture_output=True, text=True, timeout=5)
                rows = [line.split('|', 4) for line in response.stdout.strip().splitlines() if line.count('|') == 4]
            if rows:
                states = [row[1] for row in rows]
                running = [row for row in rows if row[1] == 'RUNNING']
                if running:
                    self.value = {'state': 'RUNNING', 'elapsed': max(slurm_seconds(r[2]) or 0 for r in running),
                                  'node': f'{len(running)} part(s) running', 'reason': '',
                                  'pending': states.count('PENDING')}
                else:
                    self.value = {'state': 'PENDING', 'elapsed': None, 'node': '', 'reason': rows[0][4]}
                return self.value
            if shutil.which('sacct'):
                response = subprocess.run(['sacct', '-X', '-j', jobs, '-nP', '--format=State,ExitCode,ElapsedRaw,NodeList'],
                                          capture_output=True, text=True, timeout=5)
                lines = [line.split('|') for line in response.stdout.strip().splitlines() if line.strip()]
                if response.returncode == 0 and lines:
                    states = [line[0].split()[0].rstrip('+') for line in lines]
                    bad = [(st, line[1]) for st, line in zip(states, lines) if st in BAD_STATES]
                    state = bad[0][0] if bad else 'COMPLETED' if all(st == 'COMPLETED' for st in states) else states[0]
                    self.value = {'state': state, 'exit_code': bad[0][1] if bad else '0:0',
                                  'elapsed': max(int(line[2]) for line in lines), 'node': ''}
                    return self.value
            self.value = {'error': 'Scheduler state unavailable; showing saved run state.'}
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            self.value = {'error': 'Scheduler query unavailable; showing saved run state.'}
        return self.value


def merge_counts(items):
    """Add the summary counts of parallel parts."""
    items = [item for item in items if item.get('available')]
    if not items:
        return {}
    total = {'available': True, 'caught_up': all(item['caught_up'] for item in items),
             'last_growth': max([item['last_growth'] for item in items if item.get('last_growth')] or [None]),
             'aliases': {}}
    for key in ('parents', 'records', 'passing', 'failing', 'kept', 'unclassified', 'indexed_bytes', 'summary_bytes'):
        total[key] = sum(item.get(key, 0) for item in items)
    for item in items:
        for alias, number in item.get('aliases', {}).items():
            total['aliases'][alias] = total['aliases'].get(alias, 0) + number
    return total


class SummaryIndex:
    def __init__(self, database, keep_unclassified=True):
        database.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(database), timeout=2)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA cache_size=-8192')
        self.db.execute('CREATE TABLE IF NOT EXISTS parents (id BLOB PRIMARY KEY) WITHOUT ROWID')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, value TEXT)')
        self.db.commit()
        self.keep_unclassified = keep_unclassified

    def close(self):
        self.db.close()

    def advance(self, path, max_bytes=16 * 1024 * 1024):
        with path.open('rb') as stream, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            stat = os.fstat(stream.fileno())
            header_bytes = stream.readline()
            if not header_bytes.endswith(b'\n'):
                return {'available': False, 'caught_up': False}
            header = header_bytes.decode('utf-8-sig').rstrip('\r\n').split('\t')
            required = ('parent_read_id', 'read_id', 'passes_filtering', 'alias')
            if not all(key in header for key in required):
                raise UserError('This Dorado summary lacks parent IDs, filtering or aliases; numeric progress is unavailable.')
            columns = {key: header.index(key) for key in required}
            row = self.db.execute('SELECT value FROM state WHERE id=1').fetchone()
            saved = json.loads(row[0]) if row else {}
            identity = [stat.st_dev, stat.st_ino, header_bytes.decode('utf-8-sig')]
            if saved.get('identity') != identity or stat.st_size < saved.get('offset', 0):
                self.db.execute('DELETE FROM parents')
                saved = {'identity': identity, 'offset': len(header_bytes), 'parents': 0,
                         'records': 0, 'passing': 0, 'failing': 0, 'kept': 0,
                         'unclassified': 0, 'aliases': {}, 'last_growth': None}
            start = saved['offset']
            stream.seek(start)
            identifiers = []
            records_before = saved['records']
            reached_end = False
            while stream.tell() - start < max_bytes:
                offset = stream.tell()
                line = stream.readline()
                if not line or not line.endswith(b'\n'):
                    stream.seek(offset)
                    reached_end = True
                    break
                values = line.rstrip(b'\r\n').split(b'\t')
                if len(values) != len(header):
                    raise UserError('An incomplete or malformed summary row prevents progress counting.')
                parent = values[columns['parent_read_id']] or values[columns['read_id']]
                try:
                    identity_bytes = bytes.fromhex(parent.decode('ascii').replace('-', ''))
                except (ValueError, UnicodeError):
                    raise UserError('An invalid parent read ID prevents reliable progress counting.')
                if len(identity_bytes) != 16:
                    raise UserError('An invalid parent read ID prevents reliable progress counting.')
                identifiers.append((identity_bytes,))
                passing = values[columns['passes_filtering']].upper()
                if passing not in (b'TRUE', b'FALSE', b'1', b'0'):
                    raise UserError('Unrecognised quality-filter flag in the sequencing summary.')
                passed = passing in (b'TRUE', b'1')
                alias = values[columns['alias']].decode('utf-8')
                saved['records'] += 1
                saved['passing' if passed else 'failing'] += 1
                if passed and (self.keep_unclassified or alias != 'unclassified'):
                    saved['kept'] += 1
                    saved['aliases'][alias] = saved['aliases'].get(alias, 0) + 1
                    if alias == 'unclassified':
                        saved['unclassified'] += 1
            changes = self.db.total_changes
            self.db.executemany('INSERT OR IGNORE INTO parents VALUES (?)', identifiers)
            saved['parents'] += self.db.total_changes - changes
            saved['offset'] = stream.tell()
            if saved['records'] > records_before:
                saved['last_growth'] = stat.st_mtime
            self.db.execute('INSERT OR REPLACE INTO state VALUES (1, ?)', (json.dumps(saved),))
        size = path.stat().st_size
        # A writer may leave one partial line at EOF; wait for it next refresh.
        caught_up = reached_end or saved['offset'] >= size
        return dict(saved, available=True, caught_up=caught_up,
                    indexed_bytes=saved['offset'], summary_bytes=size)

def file_totals(directory, suffixes):
    files, size, partial = 0, 0, 0
    if directory.is_dir():
        for path in directory.iterdir():
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if path.is_file() and path.name.endswith(suffixes):
                files += 1
                size += stat.st_size
            elif path.is_file() and path.name.endswith('.partial'):
                partial += 1
                size += stat.st_size
    return {'files': files, 'bytes': size, 'partial': partial}


class Monitor:
    def __init__(self, root):
        self.root = root
        self.config = read_json(root / 'config/run.json')
        if not self.config or not (run_file(root, 'status.json')).is_file():
            raise UserError('This is not a run prepared by haplocaller.')
        self.submission = read_json(run_file(root, 'submission.json'))
        self.scheduler = Scheduler(self.submission.get('job_id'), self.submission.get('merge_job_id'))
        self.index = None
        self.parts = {}

    def close(self):
        for index in (self.index or {}).values():
            index.close()

    def snapshot(self):
        cfg = self.config
        submission = read_json(run_file(self.root, 'submission.json'))
        if submission.get('job_id') != self.submission.get('job_id'):
            self.submission = submission
            self.scheduler = Scheduler(submission.get('job_id'), submission.get('merge_job_id'))
            self.config = cfg = read_json(self.root / 'config/run.json')
        scheduler = self.scheduler.get()
        state = read_json(run_file(self.root, 'status.json'))
        report = read_json(run_file(self.root, 'verification.json'))
        phase = state.get('state', 'unknown')
        counts_path = run_file(self.root, 'sample_counts.tsv').relative_to(self.root)
        message = state.get('message', '').replace('counts: sample_counts.tsv', f'counts: {counts_path}')
        recovered = phase in FINISHED and revalidated(report, self.submission.get('job_id'))
        if scheduler.get('state') in BAD_STATES and not recovered:
            phase = 'failed'
            message = f'Slurm ended the job: {scheduler["state"]} (exit {scheduler.get("exit_code", "unknown")}).'
        elif scheduler.get('state') == 'PENDING':
            phase = 'queued'
            message = scheduler.get('reason', 'Waiting for resources.')
        elif scheduler.get('state') == 'COMPLETED' and (phase not in FINISHED or report.get('verification') != 'passed'):
            phase = 'incomplete'
            message = 'Slurm finished, but successful output verification is missing. Check the logs.'
        elif phase in FINISHED and report.get('verification') != 'passed':
            phase = 'incomplete'
            message = 'Run is marked complete, but successful output verification is missing.'
        total = int(cfg['probe']['reads'])
        if cfg['mode'] == 'pilot':
            total = min(1000, total)
        shards = cfg.get('shards') if cfg['mode'] == 'full' and len(cfg.get('shards') or []) > 1 else None
        summaries = ([self.root / f'tmp/native_{k}/sequencing_summary.txt' for k in range(len(shards))]
                     if shards else [self.root / 'tmp/native/sequencing_summary.txt'])
        summary = summaries[0]
        counts, warning = {}, ''
        if phase in FINISHED:
            counts = {'available': True, 'caught_up': True,
                      'parents': report.get('input_raw_reads', report.get('raw_reads_basecalled', total)),
                      'records': report['basecall_records'], 'kept': report['kept_reads'],
                      'failing': report['discarded_quality'],
                      'internally_filtered': report.get('dorado_filtered_records', 0)}
            rows = run_file(self.root, 'sample_counts.tsv')
            if rows.is_file():
                import csv
                with rows.open() as stream:
                    records = list(csv.DictReader(stream, delimiter='\t'))
                counts['unclassified'] = sum(int(r['reads']) for r in records if r['alias'] == 'unclassified')
        elif any(path.is_file() for path in summaries):
            try:
                if self.index is None:
                    identity = str(self.root) + '|' + str(self.submission.get('job_id', cfg.get('created_at')))
                    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
                    self.index = {}
                    self.database = Path(cfg['profile']['work_root']) / '.basecall/watch' / key
                for number, path in enumerate(summaries):
                    if path.is_file():
                        if number not in self.index:
                            self.index[number] = SummaryIndex(Path(str(self.database) + (f'_{number}' if shards else '') + '.sqlite3'),
                                                              cfg['unclassified'] == 'keep')
                        self.parts[number] = self.index[number].advance(path)
                counts = merge_counts(list(self.parts.values())) if shards else self.parts.get(0, {})
            except (UserError, OSError, ValueError, sqlite3.Error) as error:
                warning = 'Progress unavailable: ' + str(error)
        done = counts.get('parents', 0)
        if recovered and scheduler.get('state') in BAD_STATES:
            warning = ('Outputs revalidated successfully. Original Slurm job remains '
                       + scheduler['state'] + ' in accounting history.')
        if done > total:
            warning = 'Summary contains more distinct raw read IDs than the input manifest; check the run.'
        elapsed = scheduler.get('elapsed')
        rate = done / elapsed if elapsed and elapsed >= 30 and counts.get('caught_up') and done else None
        eta = (total - done) / rate if rate and total > done and phase == 'basecalling' else None
        if counts.get('last_growth') and time.time() - counts['last_growth'] > 120:
            eta = None
        output = file_totals(self.root / cfg['output'], ('.fastq.gz', '.bam'))
        latest = [line for line in tail(run_file(self.root, 'run.log')).splitlines() if line.strip()]
        return {'config': cfg, 'job': self.submission.get('job_id'), 'phase': phase, 'message': message,
                'scheduler': scheduler, 'counts': counts, 'total': total, 'elapsed': elapsed, 'rate': rate,
                'eta': eta, 'output': output, 'warning': warning,
                'free_bytes': shutil.disk_usage(self.root).free,
                'latest': latest[-1] if latest else 'Waiting for the job to start.',
                'finished': phase in FINISHED | STOPPED | {'ready'}}


def render(data):
    cfg, counts, phase = data['config'], data['counts'], data['phase']
    width = max(30, min(54, shutil.get_terminal_size((90, 24)).columns - 24))
    style = 'ok' if phase in FINISHED else 'error' if phase in STOPPED else 'warning' if phase == 'queued' else 'accent'
    lines = [ui.paint('HAPLOCALLER  /  ' + cfg['name'], 'title'),
             ui.paint(phase.replace('_', ' ').upper(), style) + '   Job ' + str(data['job'] or 'not submitted')
             + '   ' + ui.clean(data['scheduler'].get('node') or cfg['profile']['name']),
             f'{cfg["tier"].upper()}  |  Q{cfg["min_qscore"]:g}  |  '
             + (f'{len(cfg["shards"])} parts × {cfg["gpus"]} GPU(s)' if cfg['mode'] == 'full' and len(cfg.get('shards') or []) > 1 else f'{cfg["gpus"]} GPU(s)') + '  |  '
             + ('FULL DATASET' if cfg['mode'] == 'full' else 'SMALL TEST'), '']
    if counts.get('available'):
        done, total = counts['parents'], data['total']
        percent = min(100, 100 * done / total) if total else 0
        if phase in FINISHED:
            lines += [ui.paint('OUTPUT VERIFIED', 'label'),
                      ui.bar(1, 1, width, style),
                      f'Input: {total:,} raw reads. Export and Dorado counter checks passed.']
        elif not counts.get('caught_up'):
            size = counts.get('summary_bytes', 0)
            fraction = 100 * counts.get('indexed_bytes', 0) / size if size else 0
            lines += [ui.paint('READING PROGRESS HISTORY', 'warning') + f'   {fraction:.0f}%',
                      ui.bar(counts.get('indexed_bytes', 0), size, width, 'warning'),
                      f'{done:,} raw read IDs counted so far; target {total:,}.',
                      ui.paint('Catching up with existing summary records; ETA will follow.', 'muted')]
        else:
            lines += [ui.paint('BASECALLING', 'label') + f'   {percent:5.1f}%',
                      ui.bar(done, total, width, style),
                      f'{done:,} / {total:,} raw read IDs represented in output']
        lines += ['', f'Elapsed {duration(data["elapsed"])}   |   Estimated time left {duration(data["eta"])}',
                  f'Basecall records {counts["records"]:,}   |   Kept {counts["kept"]:,}   |   Below Q{cfg["min_qscore"]:g} {counts["failing"]:,}']
        if 'unclassified' in counts:
            lines += [f'Kept unclassified {counts["unclassified"]:,}']
        if counts.get('internally_filtered'):
            lines += [f'Dorado internally filtered {counts["internally_filtered"]:,} segments before output']
        if data['rate'] and phase == 'basecalling':
            lines += [f'Average {data["rate"]:,.0f} raw reads/s (includes startup); ETA is approximate.']
    else:
        lines += [ui.paint('BASECALLING', 'label'), f'Target: {data["total"]:,} raw reads',
                  'Waiting for the first sequencing-summary records.', f'Elapsed {duration(data["elapsed"])}']
    output = data['output']
    lines += ['', ui.paint('FINAL FILES', 'label') + f'   {output["files"]} ready  /  {human_size(output["bytes"])}'
              + '   |   Free ' + human_size(data['free_bytes'])]
    if phase == 'exporting':
        expected = len(counts.get('aliases', {}))
        if expected and counts.get('caught_up'):
            lines += [ui.bar(output['files'], expected, width) + f' {output["files"]}/{expected} samples verified']
        lines += [f'{output["partial"]} files being written or verified; the job is still working.']
    elif phase == 'basecalling':
        lines += [ui.paint('Final files are assembled and verified after basecalling.', 'muted')]
    lines += [ui.clean(str(Path(cfg['run_dir']) / cfg['output']))]
    if data['message'] and phase != 'basecalling':
        lines += ['', ui.paint(data['message'], style)]
    if data['warning'] or data['scheduler'].get('error'):
        lines += [ui.paint(data['warning'] or data['scheduler']['error'], 'warning')]
    if phase in STOPPED:
        lines += ['Details: ' + str(Path(cfg['run_dir']) / ('logs/basecaller_*.log' if cfg['mode'] == 'full' and len(cfg.get('shards') or []) > 1 else 'logs/basecaller.log'))]
    columns = shutil.get_terminal_size((90, 24)).columns
    latest = ui.clean(data['latest'])
    if len(latest) >= columns:
        latest = latest[:max(10, columns - 4)] + '…'
    lines += ['', ui.paint(latest, 'muted')]
    return '\n'.join(lines)


def watch(root, interval=3, once=False):
    import math
    if not math.isfinite(interval) or interval < 1:
        raise UserError('Choose a refresh interval of at least one second.')
    monitor = Monitor(root)
    live = not once and sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'
    frame = None
    try:
        if live:
            print('\033[?1049h\033[?25l', end='', flush=True)
        while True:
            data = monitor.snapshot()
            frame = render(data)
            if live:
                print('\033[H\033[2J' + frame + '\n\n' + ui.paint(
                    f'Refresh {interval:g}s  |  Ctrl+C stops watching.', 'muted'), end='', flush=True)
            else:
                print(frame)
            if not live or data['finished']:
                break
            # Catch up promptly on first open; thereafter only inspect new rows
            # at the requested refresh rate. Scheduler queries stay throttled.
            catching_up = data['counts'].get('available') and not data['counts'].get('caught_up')
            time.sleep(min(interval, 0.05) if catching_up else interval)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.close()
        if live:
            print('\033[?25h\033[?1049l', end='', flush=True)
            if frame:
                print(frame)
