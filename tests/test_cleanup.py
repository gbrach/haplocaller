import json
import os
import contextlib
import importlib.util
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from haplocaller.cleanup import cleanup_run
from haplocaller.common import run_file, UserError, write_json
from haplocaller.worker import export_results


def completed_run(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'config').mkdir()
    (root / 'logs').mkdir()
    source = root.parent / 'source.pod5'
    source.write_bytes(b'raw signal')
    native = root / 'tmp/native'
    for group in ('fastq_pass', 'fastq_fail'):
        folder = native / 'run' / group / 'sample_A'
        folder.mkdir(parents=True)
        (folder / 'chunk.fastq').write_bytes(b'@read_' + group.encode() + b'\nACGT\n+\nIIII\n')
    (native / 'sequencing_summary.txt').write_text('temporary summary\n')
    for folder in ('models', 'code', 'tmp/cache'):
        (root / folder).mkdir(parents=True, exist_ok=True)
        (root / folder / 'temporary').write_bytes(b'temporary')
    (root / 'job.sh').write_text('job script\n')
    (root / 'submission.lock').write_text('submitted\n')
    (root / 'config/aliases.tsv').write_text('barcode\talias\nbarcode01\tsample_A\n')
    cfg = {'name': 'test', 'run_dir': str(root), 'tier': 'fast', 'min_qscore': 7, 'gpus': 1,
           'output': 'fastq', 'mapping': [{'barcode': 'barcode01', 'alias': 'sample_A'}],
           'unclassified': 'keep', 'profile': {'name': 'test', 'cpus': 2, 'minimum_free_gib': 0},
           'original_input': str(source), 'stage_input': False,
           'probe': {'input': str(source), 'reads': 2, 'files': [{'path': str(source),
                     'bytes': source.stat().st_size, 'mtime_ns': source.stat().st_mtime_ns}]}, 'mode': 'full',
           'model_name': 'test_model', 'software': {'version': 'test'}}
    write_json(root / 'config/run.json', cfg)
    log = root / 'logs/basecaller.log'
    log.write_text('Simplex reads basecalled: 2\n> 2 reads demuxed @ classifications/s: 2\n')
    export_results(root, cfg, native, log, 2)
    write_json(run_file(root, 'status.json'), {'state': 'complete', 'message': 'Finished; counts: sample_counts.tsv.'})
    run_file(root, 'run.log').write_text('Finished.\n')
    return native


class CleanupTests(unittest.TestCase):
    def test_minimal_output_preserves_results_config_logs_and_external_pod5(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'run'
            native = completed_run(root)
            retained = [root.parent / 'source.pod5', root / 'config/aliases.tsv',
                        root / 'fastq/sample_A.fastq.gz', run_file(root, 'sample_counts.tsv'),
                        run_file(root, 'verification.json'), root / 'logs/basecaller.log']
            before = {path: path.read_bytes() for path in retained}
            result = cleanup_run(root)
            self.assertGreaterEqual(result['files_removed'], 8)
            self.assertGreater(result['bytes_removed'], 0)
            self.assertEqual({p.name for p in root.iterdir()}, {'fastq', 'logs', 'config'})
            self.assertEqual(before, {path: path.read_bytes() for path in retained})
            self.assertEqual(json.loads((root / 'logs/cleanup.json').read_text())['state'], 'complete')
            self.assertEqual(cleanup_run(root)['files_removed'], 0)

    def test_failed_or_unverified_run_keeps_all_chunks(self):
        for failure in ('failed', 'unverified'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / 'run'
                native = completed_run(root)
                if failure == 'failed':
                    write_json(run_file(root, 'status.json'), {'state': 'failed'})
                else:
                    write_json(run_file(root, 'verification.json'), {'verification': 'failed'})
                with self.assertRaises(UserError):
                    cleanup_run(root)
                self.assertEqual(len(list(native.rglob('*.fastq'))), 2)

    def test_missing_modified_or_replaced_final_output_keeps_all_chunks(self):
        for change in ('missing', 'modified', 'symlink', 'counts'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / 'run'
                native = completed_run(root)
                output = root / 'fastq/sample_A.fastq.gz'
                if change == 'missing':
                    output.unlink()
                elif change == 'modified':
                    output.write_bytes(b'truncated')
                    future = (run_file(root, 'verification.json')).stat().st_mtime_ns + 1_000_000_000
                    os.utime(output, ns=(future, future))
                elif change == 'symlink':
                    output.unlink()
                    output.symlink_to(root.parent / 'source.pod5')
                else:
                    rows = json.loads((root / 'logs/export_counts.json').read_text())
                    rows[0]['reads'] += 1
                    write_json(root / 'logs/export_counts.json', rows)
                with self.assertRaises(UserError):
                    cleanup_run(root)
                self.assertEqual(len(list(native.rglob('*.fastq'))), 2)

    def test_symlink_in_native_tree_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'run'
            native = completed_run(root)
            (native / 'linked').symlink_to(root / 'fastq', target_is_directory=True)
            with self.assertRaisesRegex(UserError, 'symbolic link'):
                cleanup_run(root)
            self.assertEqual(len(list(native.rglob('*.fastq'))), 2)
            self.assertTrue((root / 'fastq/sample_A.fastq.gz').is_file())

    def test_active_job_or_scheduler_failure_keeps_all_chunks(self):
        for code, stdout in ((0, '123\n456\n'), (1, '')):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / 'run'
                native = completed_run(root)
                write_json(run_file(root, 'submission.json'), {'job_id': '123'})
                response = subprocess.CompletedProcess([], code, stdout, '')
                with patch('haplocaller.cleanup.subprocess.run', return_value=response):
                    with self.assertRaises(UserError):
                        cleanup_run(root)
                self.assertEqual(len(list(native.rglob('*.fastq'))), 2)

    def test_legacy_metadata_migrates_and_status_watch_logs_still_work(self):
        from haplocaller import cli
        from haplocaller.watch import Monitor
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'run'
            completed_run(root)
            names = ('status.json', 'verification.json', 'sample_counts.tsv', 'run.log')
            for name in names:
                (root / 'logs' / name).rename(root / name)
            write_json(root / 'submission.json', {'job_id': '123'})
            before = {name: (root / name).read_bytes() for name in names}
            with patch('haplocaller.cleanup.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')):
                cleanup_run(root)
            self.assertEqual({p.name for p in root.iterdir()}, {'fastq', 'logs', 'config'})
            self.assertEqual(before, {name: (root / 'logs' / name).read_bytes() for name in names})
            with patch('haplocaller.watch.Scheduler.get', return_value={'state': 'COMPLETED'}):
                monitor = Monitor(root)
                data = monitor.snapshot()
                monitor.close()
            self.assertEqual(data['phase'], 'complete')
            self.assertEqual(data['counts']['kept'], 1)
            self.assertIn('counts: logs/sample_counts.tsv', data['message'])
            with patch.object(cli, 'command', return_value=subprocess.CompletedProcess([], 0, 'COMPLETED', '')), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                cli.status(root)
            self.assertIn(str(root / 'logs/run.log'), output.getvalue())
            self.assertIn('counts: logs/sample_counts.tsv', output.getvalue())
            with patch('sys.argv', ['haplocaller', 'logs', str(root)]), \
                    patch.object(cli.subprocess, 'run') as tail:
                cli.main()
            self.assertEqual(Path(tail.call_args.args[0][-1]), (root / 'logs/run.log').resolve())

    def test_staging_copies_removed_only_when_original_is_unchanged(self):
        for original_ok in (True, False):
            with self.subTest(original_ok=original_ok), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / 'run'
                completed_run(root)
                cfg = json.loads((root / 'config/run.json').read_text())
                original = Path(cfg['original_input'])
                (root / 'input').mkdir()
                staged = root / 'input/source.pod5'
                shutil.copy2(original, staged)
                cfg['stage_input'] = True
                cfg['probe']['input'] = str(root / 'input')
                cfg['probe']['files'][0]['path'] = str(staged)
                write_json(root / 'config/run.json', cfg)
                if original_ok:
                    cleanup_run(root)
                    self.assertEqual(original.read_bytes(), b'raw signal')
                    self.assertFalse(staged.exists())
                else:
                    original.unlink()
                    with self.assertRaisesRegex(UserError, 'Original POD5'):
                        cleanup_run(root)
                    self.assertEqual(staged.read_bytes(), b'raw signal')
                    self.assertTrue((root / 'models/temporary').is_file())

    def test_unexpected_raw_pod5_in_tmp_blocks_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'run'
            native = completed_run(root)
            (native / 'extra.pod5').write_bytes(b'raw')
            with self.assertRaisesRegex(UserError, 'Unexpected POD5'):
                cleanup_run(root)
            self.assertEqual((native / 'extra.pod5').read_bytes(), b'raw')

    @unittest.skipUnless(importlib.util.find_spec('pysam'), 'pysam available on cluster')
    def test_bam_results_survive_removal_of_both_temporary_formats(self):
        import pysam
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'run'
            native = completed_run(root)
            shutil.rmtree(root / 'fastq')
            cfg = json.loads((root / 'config/run.json').read_text())
            cfg['output'] = 'bam'
            write_json(root / 'config/run.json', cfg)
            for quality in ('pass', 'fail'):
                folder = native / ('bam_' + quality) / 'sample_A'
                folder.mkdir(parents=True)
                with pysam.AlignmentFile(str(folder / 'chunk.bam'), 'wb', header={'HD': {'VN': '1.6'}}) as stream:
                    read = pysam.AlignedSegment(stream.header)
                    read.query_name = quality
                    read.flag = 4
                    read.query_sequence = 'ACGT'
                    read.query_qualities = pysam.qualitystring_to_array('IIII')
                    stream.write(read)
            export_results(root, cfg, native, root / 'logs/basecaller.log', 2)
            output = root / 'bam/sample_A.bam'
            before = output.read_bytes()
            cleanup_run(root)
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual({p.name for p in root.iterdir()}, {'bam', 'logs', 'config'})


if __name__ == '__main__':
    unittest.main()
