import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from haplocaller import cli, ui
from haplocaller.common import UserError, write_json
from haplocaller.watch import Monitor, SummaryIndex, render, watch

HEADER = 'parent_read_id\tread_id\tpasses_filtering\talias\n'


def row(parent, child, passed=True, alias='sample_A'):
    return f'{parent:032x}\t{child:032x}\t{"TRUE" if passed else "FALSE"}\t{alias}\n'


def make_run(root):
    (root / 'config').mkdir()
    (root / 'logs').mkdir()
    config = {'name': 'real_run', 'run_dir': str(root), 'mode': 'full', 'gpus': 2,
              'min_qscore': 7, 'output': 'fastq', 'unclassified': 'keep', 'tier': 'fast',
              'probe': {'reads': 10}, 'profile': {'work_root': str(root), 'name': 'test'}}
    write_json(root / 'config/run.json', config)
    write_json(root / 'status.json', {'state': 'basecalling', 'message': 'Processing reads.'})
    write_json(root / 'submission.json', {'job_id': '12345'})
    summary = root / 'tmp/native/sequencing_summary.txt'
    summary.parent.mkdir(parents=True)
    summary.write_text(HEADER + row(1, 1) + row(2, 2, False))
    return config, summary


class SummaryTests(unittest.TestCase):
    def test_split_reads_partial_lines_and_reopening(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = root / 'summary.txt'
            partial = row(3, 30)
            summary.write_text(HEADER + row(1, 10) + row(1, 11, False) +
                               row(2, 20, alias='unclassified') + partial[:20])
            database = root / 'cache.sqlite3'
            index = SummaryIndex(database, keep_unclassified=False)
            try:
                value = index.advance(summary)
                self.assertEqual((value['parents'], value['records']), (2, 3))
                self.assertEqual((value['kept'], value['failing'], value['passing']), (1, 1, 2))
                self.assertEqual(value['unclassified'], 0)
                unchanged = index.advance(summary)
                self.assertEqual(unchanged['records'], 3)
            finally:
                index.close()
            with summary.open('a') as stream:
                stream.write(partial[20:])
            reopened = SummaryIndex(database, keep_unclassified=False)
            try:
                value = reopened.advance(summary)
                self.assertEqual((value['parents'], value['records'], value['kept']), (3, 4, 2))
                self.assertTrue(value['caught_up'])
            finally:
                reopened.close()

    def test_bounded_catchup_and_truncated_summary_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = root / 'summary.txt'
            summary.write_text(HEADER + ''.join(row(i, i) for i in range(20)))
            index = SummaryIndex(root / 'cache.sqlite3')
            try:
                value = index.advance(summary, max_bytes=160)
                self.assertFalse(value['caught_up'])
                self.assertLess(value['parents'], 20)
                value = index.advance(summary)
                self.assertEqual(value['parents'], 20)
                summary.write_text(HEADER + row(40, 40))
                value = index.advance(summary)
                self.assertEqual((value['parents'], value['records']), (1, 1))
            finally:
                index.close()

    def test_unknown_schema_does_not_fabricate_percentages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summary = root / 'summary.txt'
            summary.write_text('read_id\tqscore\nabc\t10\n')
            index = SummaryIndex(root / 'cache.sqlite3')
            try:
                with self.assertRaisesRegex(UserError, 'numeric progress is unavailable'):
                    index.advance(summary)
            finally:
                index.close()


class MonitorTests(unittest.TestCase):
    def test_reads_and_retention_with_readonly_run_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg, summary = make_run(root)
            before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            monitor = Monitor(root)
            try:
                with patch.object(monitor.scheduler, 'get', return_value={'state': 'RUNNING', 'elapsed': 60, 'node': 'gpu1'}):
                    value = monitor.snapshot()
                self.assertEqual(value['counts']['parents'], 2)
                self.assertEqual(value['counts']['kept'], 1)
                self.assertEqual(value['counts']['failing'], 1)
                self.assertAlmostEqual(value['eta'], 240)
                self.assertEqual(value['phase'], 'basecalling')
                for path, data in before.items():
                    self.assertEqual(Path(path).read_bytes(), data)
                with patch.object(ui, 'MODE', 'never'):
                    text = render(value)
                self.assertIn('20.0%', text)
                self.assertNotIn('\033', text)
            finally:
                monitor.close()

    def test_scheduler_failure_overrides_stale_running_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_run(root)
            monitor = Monitor(root)
            try:
                with patch.object(monitor.scheduler, 'get', return_value={'state': 'OUT_OF_MEMORY', 'exit_code': '0:125'}):
                    value = monitor.snapshot()
                self.assertEqual(value['phase'], 'failed')
                self.assertTrue(value['finished'])
                self.assertIsNone(value['eta'])
                self.assertIn('OUT_OF_MEMORY', value['message'])
            finally:
                monitor.close()

    def test_scheduler_completed_requires_verified_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_run(root)
            monitor = Monitor(root)
            try:
                with patch.object(monitor.scheduler, 'get', return_value={'state': 'COMPLETED'}):
                    value = monitor.snapshot()
                self.assertEqual(value['phase'], 'incomplete')
                write_json(root / 'status.json', {'state': 'complete'})
                write_json(root / 'verification.json', {'verification': 'passed', 'raw_reads_basecalled': 10,
                           'basecall_records': 11, 'kept_reads': 9, 'discarded_quality': 2})
                with patch.object(monitor.scheduler, 'get', return_value={'state': 'COMPLETED', 'elapsed': 100}):
                    value = monitor.snapshot()
                self.assertEqual(value['phase'], 'complete')
                self.assertEqual(value['counts']['parents'], 10)
                self.assertIsNone(value['eta'])
            finally:
                monitor.close()

    def test_queued_reason_and_no_read_progress_yet(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, summary = make_run(root)
            summary.unlink()
            monitor = Monitor(root)
            try:
                with patch.object(monitor.scheduler, 'get', return_value={'state': 'PENDING', 'reason': 'Resources'}):
                    value = monitor.snapshot()
                self.assertEqual(value['phase'], 'queued')
                self.assertEqual(value['message'], 'Resources')
                self.assertFalse(value['counts'])
                self.assertFalse(value['finished'])
            finally:
                monitor.close()

    def test_revalidated_output_overrides_only_matching_historical_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_run(root)
            write_json(root / 'status.json', {'state': 'complete'})
            report = {'verification': 'passed', 'input_raw_reads': 10,
                      'basecall_records': 11, 'kept_reads': 9, 'discarded_quality': 2,
                      'dorado_filtered_records': 3,
                      'recovery': {'method': 'existing_outputs', 'original_job_id': '12345'}}
            write_json(root / 'verification.json', report)
            monitor = Monitor(root)
            try:
                with patch.object(monitor.scheduler, 'get', return_value={'state': 'FAILED', 'exit_code': '1:0'}):
                    value = monitor.snapshot()
                    self.assertEqual(value['phase'], 'complete')
                    self.assertIn('OUTPUT VERIFIED', render(value))
                    self.assertIn('Original Slurm', value['warning'])
                    self.assertIn('internally filtered 3', render(value))
                    report['recovery']['original_job_id'] = 'another_job'
                    write_json(root / 'verification.json', report)
                    self.assertEqual(monitor.snapshot()['phase'], 'failed')
            finally:
                monitor.close()

    def test_ctrl_c_restores_terminal_without_cancelling_job(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_run(root)
            terminal = Terminal()
            with patch('haplocaller.watch.Scheduler.get', return_value={'state': 'RUNNING', 'elapsed': 60}), \
                 patch('haplocaller.watch.time.sleep', side_effect=KeyboardInterrupt), \
                 patch.dict(os.environ, {'TERM': 'xterm'}), contextlib.redirect_stdout(terminal):
                watch(root)
            text = terminal.getvalue()
            self.assertIn('\033[?1049h', text)
            self.assertIn('\033[?25h\033[?1049l', text)
            self.assertEqual(json.loads((root / 'status.json').read_text())['state'], 'basecalling')


class DefaultsAndColourTests(unittest.TestCase):
    def test_no_color_and_redirected_output(self):
        with patch.object(ui, 'MODE', 'always'), patch.dict(os.environ, {'NO_COLOR': '1'}):
            self.assertEqual(ui.paint('hello'), 'hello')
        with patch.object(ui, 'MODE', 'auto'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.paint('hello'), 'hello')

    def test_sup_default_and_remembered_quality(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, table = root / 'input.pod5', root / 'samples.csv'
            source.touch()
            table.write_text('barcode,alias\nbarcode01,sample_A\n')
            (root / '.basecall').mkdir()
            write_json(root / '.basecall/last_choices.json', {'tier': 'fast', 'min_qscore': 11})
            profile = {'name': 'test', 'work_root': str(root), 'default_gpus': 2}
            info = {'input': str(source), 'reads': 10, 'bytes': 1000, 'kit': 'SQK-NBD114-96',
                    'files': [{'path': str(source)}]}
            with patch.object(cli, 'check_profile'), patch.object(cli, 'read_input', return_value=info), \
                 patch.object(cli, 'path_input', side_effect=[source, table]), \
                 patch.object(cli, 'select_software', return_value={'version': '2.1.0'}), \
                 patch('builtins.input', return_value=''), contextlib.redirect_stdout(io.StringIO()):
                cfg = cli.collect(profile, SimpleNamespace(no_update_check=True))
            self.assertEqual(cfg['tier'], 'sup')
            self.assertEqual(cfg['min_qscore'], 11)
            self.assertEqual(cfg['runtime_days'], 14)
            self.assertEqual(cfg['output'], 'fastq')


if __name__ == '__main__':
    unittest.main()
