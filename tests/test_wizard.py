import contextlib
import gzip
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from haplocaller import cli
from haplocaller.common import run_file, UserError, mapping_tables, write_json
from haplocaller.worker import basecaller_command, export_results


def config(root):
    source = root / 'input.pod5'
    source.write_bytes(b'placeholder')
    return {'name': 'test_run', 'run_dir': str(root / 'test_run'), 'mode': 'pilot',
            'profile': {'name': 'test', 'work_root': str(root), 'account': 'lab', 'partition': 'gpu',
                        'gres': 'gpu:tesla:{gpus}', 'cpus': 2, 'memory': '8G', 'time': '14-00:00:00',
                        'python': 'python3', 'minimum_free_gib': 0},
            'probe': {'input': str(source), 'files': [{'path': str(source), 'bytes': 11,
                      'mtime_ns': source.stat().st_mtime_ns, 'reads': 2}], 'reads': 2, 'bytes': 11,
                      'experiment': 'plate', 'flowcell': 'PBE123'},
            'mapping': [{'barcode': 'barcode01', 'alias': 'sample_A'}],
            'mapping_label': 'samples.tsv', 'stage_input': False, 'tier': 'fast',
            'kit': 'SQK-NBD114-96', 'output': 'fastq', 'min_qscore': 10,
            'unclassified': 'discard', 'gpus': 2,
            'software': {'executable': '/usr/bin/dorado', 'module': None, 'version': '2.1.0'},
            'model_name': 'test_fast', 'created_at': 'test'}


def fq(identifier, seq='ACGT'):
    return f'@{identifier}\n{seq}\n+\n{"I"*len(seq)}\n'.encode()


class MappingTests(unittest.TestCase):
    def test_csv_duplicates_and_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            table = Path(temp) / 'aliases.csv'
            table.write_text('\ufeffbarcode,alias\nbarcode01,sample_A\nbarcode02,sample_B\n')
            self.assertEqual(len(mapping_tables(table)[0]['rows']), 2)
            table.write_text('barcode,alias\nbarcode01,sample_A\nbarcode01,sample_B\n')
            with self.assertRaisesRegex(UserError, 'repeated barcode'):
                mapping_tables(table)
            table.write_text('')
            with self.assertRaisesRegex(UserError, 'empty'):
                mapping_tables(table)

    def test_excel_is_rejected_with_conversion_guidance(self):
        with self.assertRaisesRegex(UserError, 'Export the selected Excel plate'):
            mapping_tables(Path('plate.xlsx'))


class CommandTests(unittest.TestCase):
    def test_qscore_passed_as_integer_in_pilot_and_full_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config(root)
            cfg['model_path'] = '/model'
            for mode in ('pilot', 'full'):
                cfg['mode'] = mode
                for value in (0, 0.0, 7, 7.0, '7.0'):
                    with self.subTest(mode=mode, value=value):
                        cfg['min_qscore'] = value
                        args, expected = basecaller_command(cfg, root, root / 'input.pod5', root / 'native')
                        score = args[args.index('--min-qscore') + 1]
                        self.assertEqual(int(score), int(float(value)))
                        self.assertEqual('--max-reads' in args, mode == 'pilot')
                cfg['min_qscore'] = 7.5
                with self.assertRaisesRegex(UserError, 'whole-number'):
                    basecaller_command(cfg, root, root / 'input.pod5', root / 'native')


class ExportTests(unittest.TestCase):
    def test_internally_filtered_input_does_not_fail_completed_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config(root)
            native = root / 'native/fastq_pass/sample_A'
            native.mkdir(parents=True)
            (native / 'a.fastq').write_bytes(fq('one'))
            logfile = root / 'basecaller.log'
            logfile.write_text('Simplex reads basecalled: 1\nSimplex reads filtered: 1\n'
                               '> 2 reads demuxed @ classifications/s: 2\n')
            report = export_results(root, cfg, root / 'native', logfile, 2)
            self.assertEqual(report['input_raw_reads'], 2)
            self.assertEqual(report['kept_reads'], 1)
            self.assertEqual(report['dorado_filtered_records'], 1)
            self.assertEqual(report['discarded_quality'], 0)
            self.assertTrue((root / 'logs/export_counts.json').is_file())

    def test_filtering_flat_names_and_split_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config(root)
            native = root / 'tmp/native'
            for group, alias, record in [('fastq_pass', 'sample_A', 'kept'),
                                         ('fastq_fail', 'sample_A', 'low_quality'),
                                         ('fastq_pass', 'unclassified', 'unknown')]:
                path = native / group / alias / 'calls.fastq'
                path.parent.mkdir(parents=True)
                path.write_bytes(fq(record))
            logfile = root / 'basecaller.log'
            logfile.write_text('Simplex reads basecalled: 2\n> 3 reads demuxed @ classifications/s: 3\n')
            report = export_results(root, cfg, native, logfile, 2)
            self.assertEqual(report['kept_reads'], 1)
            self.assertEqual(report['discarded_quality'], 1)
            self.assertEqual(report['discarded_unclassified'], 1)
            self.assertEqual([p.name for p in (root / 'fastq').iterdir()], ['sample_A.fastq.gz'])
            with gzip.open(root / 'fastq/sample_A.fastq.gz', 'rb') as stream:
                self.assertEqual(stream.read(), fq('kept'))
            self.assertTrue((native / 'fastq_fail/sample_A/calls.fastq').exists())

    def test_missing_output_is_not_reported_successful(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config(root)
            native = root / 'native/fastq_pass/sample_A'
            native.mkdir(parents=True)
            (native / 'a.fastq').write_bytes(fq('one'))
            logfile = root / 'basecaller.log'
            logfile.write_text('Simplex reads basecalled: 2\n> 2 reads demuxed @ classifications/s: 2\n')
            with self.assertRaisesRegex(ValueError, 'demultiplexed count'):
                export_results(root, cfg, root / 'native', logfile, 2)
            self.assertFalse((run_file(root, 'verification.json')).exists())

    @unittest.skipUnless(importlib.util.find_spec('pysam'), 'pysam available on cluster')
    def test_bam_chunks_merge_headers_and_keep_records(self):
        import pysam
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config(root)
            cfg.update(output='bam', unclassified='keep', min_qscore=0)
            native = root / 'native/bam_pass/sample_A'
            native.mkdir(parents=True)
            for index in range(2):
                header = {'HD': {'VN': '1.6'}, 'RG': [{'ID': f'group{index}', 'SM': 'sample_A'}]}
                with pysam.AlignmentFile(str(native / f'{index}.bam'), 'wb', header=header) as out:
                    read = pysam.AlignedSegment(out.header)
                    read.query_name = f'read{index}'
                    read.flag = 4
                    read.query_sequence = 'ACGT'
                    read.query_qualities = pysam.qualitystring_to_array('IIII')
                    read.set_tag('RG', f'group{index}')
                    out.write(read)
            logfile = root / 'basecaller.log'
            logfile.write_text('Simplex reads basecalled: 2\n> 2 reads demuxed @ classifications/s: 2\n')
            export_results(root, cfg, root / 'native', logfile, 2)
            with pysam.AlignmentFile(str(root / 'bam/sample_A.bam'), 'rb', check_sq=False) as stream:
                self.assertEqual(len(stream.header.to_dict()['RG']), 2)
                self.assertEqual({r.query_name for r in stream}, {'read0', 'read1'})


class SubmissionTests(unittest.TestCase):
    def test_prepare_only_builds_ready_run_and_never_calls_sbatch(self):
        with tempfile.TemporaryDirectory(prefix='basecall space ') as temp:
            cfg = config(Path(temp))
            calls = []
            def fake_command(args, *pos, **kwargs):
                calls.append(args)
                self.assertIn('download', args)
                destination = Path(args[args.index('--models-directory') + 1]) / 'dna_fast@test'
                destination.mkdir()
                (destination / 'config.toml').write_text('test = true\n')
                return subprocess.CompletedProcess(args, 0)
            with patch.object(cli, 'command', side_effect=fake_command), contextlib.redirect_stdout(io.StringIO()):
                run = cli.prepare(cfg)
            self.assertTrue((run / 'code/haplocaller/worker.py').is_file())
            self.assertEqual(json.loads((run_file(run, 'status.json')).read_text())['state'], 'ready')
            self.assertFalse((run_file(run, 'submission.json')).exists())
            self.assertTrue(all(args[0] != 'sbatch' for args in calls))
            subprocess.run(['bash', '-n', str(run / 'job.sh')], check=True)

    def test_job_can_only_be_submitted_once(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = config(Path(temp))
            cfg['min_qscore'] = 7.0
            run = Path(cfg['run_dir'])
            (run / 'config').mkdir(parents=True)
            (run / 'logs').mkdir()
            write_json(run / 'config/run.json', cfg)
            write_json(run_file(run, 'status.json'), {'state': 'ready'})
            response = subprocess.CompletedProcess(['sbatch'], 0, '12345\n', '')
            with patch.object(cli, 'command', return_value=response) as call, contextlib.redirect_stdout(io.StringIO()):
                cli.submit(run, 'pilot')
                with self.assertRaises(UserError):
                    cli.submit(run, 'full')
            self.assertEqual(call.call_count, 1)
            args = call.call_args.args[0]
            self.assertIn('--ntasks=1', args)
            self.assertIn('--gres=gpu:tesla:2', args)
            self.assertIn('--time=14-00:00:00', args)
            saved_score = json.loads((run / 'config/run.json').read_text())['min_qscore']
            self.assertEqual(saved_score, 7)
            self.assertIsInstance(saved_score, int)

    def test_prepare_flag_cannot_submit_via_start(self):
        with patch.object(sys, 'argv', ['basecall', '--prepare-only', 'start', '/tmp/test']), \
             patch.object(cli, 'submit') as submit, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main()
            submit.assert_not_called()

    def test_enter_at_action_menu_leaves_run_unsubmitted(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = config(Path(temp))
            run = Path(cfg['run_dir'])
            with patch.object(sys, 'argv', ['basecall']), patch.object(cli, 'load_profile', return_value={}), \
                 patch.object(cli, 'collect', return_value=cfg), patch.object(cli, 'preview'), \
                 patch.object(cli, 'prepare', return_value=run), patch.object(cli, 'submit') as submit, \
                 patch('builtins.input', return_value=''), contextlib.redirect_stdout(io.StringIO()):
                cli.main()
                submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
