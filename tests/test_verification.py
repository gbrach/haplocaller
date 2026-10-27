import contextlib
import gzip
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from haplocaller.common import run_file, write_json
from haplocaller.revalidate import check_inactive_job, recover, summary_counts, validate_outputs
from haplocaller.verification import dorado_counts
from test_wizard import config, fq


def log_text(primary, filtered, demux):
    return (f'Simplex reads basecalled: {primary}\n'
            + (f'Simplex reads filtered: {filtered}\n' if filtered else '')
            + f'> {demux} reads demuxed @ classifications/s: 1\n> Finished\n')


class CounterTests(unittest.TestCase):
    def test_first_and_later_split_segments_can_both_be_filtered(self):
        result = dorado_counts(log_text(3, 3, 7), expected_raw=5, written=4)
        self.assertEqual(result['raw_reads_without_primary_output'], 2)
        self.assertEqual(result['filtered_nonprimary_records'], 1)
        self.assertEqual(result['additional_split_records'], 2)

    def test_plate3_full_run_counters(self):
        result = dorado_counts(log_text(10211625, 6954, 10232008), 10218481, 10225054)
        self.assertEqual(result['filtered_nonprimary_records'], 98)
        self.assertEqual(result['additional_split_records'], 13527)
        self.assertEqual(result['raw_reads_without_primary_output'], 6856)

    def test_no_filtered_counter_means_zero(self):
        self.assertEqual(dorado_counts(log_text(2, 0, 3), 2, 3)['dorado_filtered_records'], 0)

    def test_all_first_segments_filtered_can_leave_later_segments(self):
        text = 'Simplex reads filtered: 1\n> 3 reads demuxed @ classifications/s: 1\n'
        result = dorado_counts(text, expected_raw=1, written=2)
        self.assertEqual(result['dorado_primary_reads_written'], 0)
        self.assertEqual(result['raw_reads_without_primary_output'], 1)
        self.assertEqual(result['additional_split_records'], 2)

    def test_missing_records_and_inconsistent_input_still_fail(self):
        for primary, filtered, demux, expected, written in [
            (3, 3, 7, 5, 3),  # one output record missing
            (3, 1, 5, 5, 4),  # missing input cannot be explained by filtering
            (6, 1, 7, 5, 6),  # more first segments than raw inputs
            (3, 3, 5, 5, 2),  # fewer written records than first segments
        ]:
            with self.subTest(values=(primary, filtered, demux, expected, written)):
                with self.assertRaises(ValueError):
                    dorado_counts(log_text(primary, filtered, demux), expected, written)

    def test_duplicate_or_missing_counters_are_rejected(self):
        with self.assertRaises(ValueError):
            dorado_counts(log_text(2, 1, 3) * 2, 2, 2)
        with self.assertRaises(ValueError):
            dorado_counts('Simplex reads filtered: 1\n', 2, 1)


def recovery_fixture(root):
    cfg = config(root)
    cfg['run_dir'] = str(root)
    for path in ('config', 'logs', 'tmp/native', 'fastq'):
        (root / path).mkdir(parents=True, exist_ok=True)
    write_json(root / 'config/run.json', cfg)
    write_json(run_file(root, 'submission.json'), {'job_id': '12345'})
    write_json(run_file(root, 'status.json'), {'state': 'failed',
        'message': 'Stopped during exporting: Expected 2 raw reads; Dorado reports [1].'})
    (run_file(root, 'run.log')).write_text('Basecalling finished. Creating and checking one file per sample.\n')
    (root / 'logs/basecaller.log').write_text(log_text(1, 1, 4))
    summary = root / 'tmp/native/sequencing_summary.txt'
    summary.write_text('read_id\talias\tpasses_filtering\tsequence_length_template\n'
                       'one\tsample_A\tTRUE\t4\n'
                       'two\tsample_A\tTRUE\t4\n'
                       'low_quality\tsample_A\tFALSE\t4\n')
    output = root / 'fastq/sample_A.fastq.gz'
    with gzip.open(output, 'wb') as stream:
        stream.write(fq('two') + fq('one'))  # arbitrary order is permitted
    return cfg, output, summary


class RecoveryTests(unittest.TestCase):
    def test_unrelated_live_jobs_do_not_require_accounting_access(self):
        with patch('haplocaller.revalidate.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, '99999\n99998_3\n', '')) as run:
            check_inactive_job('12345')
            self.assertEqual(run.call_count, 1)
            self.assertNotIn('-j', run.call_args.args[0])

    def test_expired_queue_entry_requires_terminal_accounting_evidence(self):
        expired = subprocess.CompletedProcess([], 1, '', 'Invalid job id specified')
        for evidence, success in [('12345|FAILED|\n', True), ('12345|RUNNING|\n', False), ('', False)]:
            with self.subTest(evidence=evidence), patch('haplocaller.revalidate.subprocess.run',
                    side_effect=[expired, subprocess.CompletedProcess([], 0, evidence, '')]):
                if success:
                    check_inactive_job('12345')
                else:
                    with self.assertRaisesRegex(ValueError, 'Cannot confirm'):
                        check_inactive_job('12345')

    def test_recovery_checks_existing_output_without_rewriting_sequences(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg, output, _ = recovery_fixture(root)
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (output, root / 'input.pod5')}
            with patch('haplocaller.revalidate.subprocess.run',
                       return_value=subprocess.CompletedProcess([], 0, '', '')), \
                 contextlib.redirect_stdout(io.StringIO()):
                report = recover(root, 1)
            self.assertEqual(report['kept_reads'], 2)
            self.assertEqual(report['discarded_quality'], 1)
            self.assertEqual(report['recovery']['original_job_id'], '12345')
            self.assertEqual(report['verification'], 'passed')
            self.assertEqual(json.loads((run_file(root, 'status.json')).read_text())['state'], 'pilot_complete')
            for path, original in before.items():
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), original)
            audit = Path(report['recovery']['audit'])
            self.assertEqual(json.loads((audit / 'previous_status.json').read_text())['state'], 'failed')
            self.assertTrue((audit / 'output_checks.json').is_file())

    def test_same_count_and_lengths_with_duplicate_or_replaced_id_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg, output, summary = recovery_fixture(root)
            with gzip.open(output, 'wb') as stream:
                stream.write(fq('one') + fq('one'))
            rows = summary_counts(summary, cfg)
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                validate_outputs(root, cfg, rows, 1)

    def test_missing_unexpected_and_truncated_files_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg, output, summary = recovery_fixture(root)
            rows = summary_counts(summary, cfg)
            output.rename(output.with_suffix('.partial'))
            with self.assertRaisesRegex(ValueError, 'file set mismatch'):
                validate_outputs(root, cfg, rows, 1)
            output.with_suffix('.partial').rename(output)
            output.write_bytes(output.read_bytes()[:-8])
            with self.assertRaises(EOFError):
                validate_outputs(root, cfg, rows, 1)

    def test_active_job_or_unrelated_failure_cannot_be_recovered(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            recovery_fixture(root)
            with patch('haplocaller.revalidate.subprocess.run',
                       return_value=subprocess.CompletedProcess([], 0, '12345\n', '')):
                with self.assertRaisesRegex(ValueError, 'still active'):
                    recover(root, 1)
            write_json(run_file(root, 'status.json'), {'state': 'failed', 'message': 'GPU failed'})
            with self.assertRaisesRegex(ValueError, 'known post-export'):
                recover(root, 1)
            self.assertFalse((run_file(root, 'verification.json')).exists())

    def test_failed_revalidation_restores_original_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, output, _ = recovery_fixture(root)
            previous = json.loads((run_file(root, 'status.json')).read_text())
            output.unlink()
            with patch('haplocaller.revalidate.subprocess.run',
                       return_value=subprocess.CompletedProcess([], 0, '', '')), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):
                    recover(root, 1)
            self.assertEqual(json.loads((run_file(root, 'status.json')).read_text()), previous)
            self.assertFalse((run_file(root, 'verification.json')).exists())


if __name__ == '__main__':
    unittest.main()
