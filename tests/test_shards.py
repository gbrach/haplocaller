import gzip
import tempfile
import unittest
from pathlib import Path

from haplocaller.common import plan_shards, slurm_args
from haplocaller.worker import export_parts
from test_wizard import config, fq


class PlanTests(unittest.TestCase):
    def test_balanced_whole_files_and_never_empty_parts(self):
        files = [{'path': f'/p/{i}.pod5', 'bytes': size} for i, size in enumerate([9, 8, 3, 3, 2, 1])]
        plan = plan_shards(files, 3)
        sizes = sorted(sum(next(f['bytes'] for f in files if f['path'] == p) for p in group) for group in plan)
        self.assertEqual(sizes, [8, 9, 9])
        self.assertEqual(sorted(p for g in plan for p in g), sorted(f['path'] for f in files))
        self.assertEqual(len(plan_shards(files[:2], 5)), 2)


class SlurmTests(unittest.TestCase):
    def test_array_for_full_sharded_run_single_job_for_test_and_cpu_merge(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = config(Path(temp))
            cfg['shards'] = [['a'], ['b'], ['c']]
            cfg['profile'].update(constraint='gpua100|gpuv100', cpu_partition='public')
            cfg['mode'] = 'full'
            args = slurm_args(cfg, 'job.sh')
            self.assertIn('--array=0-2', args)
            self.assertIn('--gres=gpu:tesla:2', args)
            self.assertTrue(any(a.endswith('slurm_%a.log') for a in args))
            cfg['mode'] = 'pilot'
            self.assertFalse(any(a.startswith('--array') for a in slurm_args(cfg, 'job.sh')))
            cfg['mode'] = 'full'
            merge = slurm_args(cfg, 'job_merge.sh', role='merge', dependency='77')
            self.assertIn('--dependency=afterok:77', merge)
            self.assertIn('--partition=public', merge)
            self.assertFalse(any(a.startswith(('--gres', '--constraint', '--array')) for a in merge))


class MergeTests(unittest.TestCase):
    def test_parts_are_merged_per_sample_and_verified_per_part(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config(root)
            cfg.update(unclassified='keep', min_qscore=0, shards=[['a'], ['b']])
            parts = []
            for index, text in enumerate(['one', 'two']):
                native = root / f'tmp/native_{index}'
                path = native / 'fastq_pass/sample_A/calls.fastq'
                path.parent.mkdir(parents=True)
                path.write_bytes(fq(text))
                log = root / f'b{index}.log'
                log.write_text('Simplex reads basecalled: 1\n> 1 reads demuxed @ classifications/s: 1\n')
                parts.append((native, log, 1))
            report = export_parts(root, cfg, parts)
            self.assertEqual(report['kept_reads'], 2)
            self.assertEqual(report['input_raw_reads'], 2)
            with gzip.open(root / 'fastq/sample_A.fastq.gz', 'rb') as stream:
                self.assertEqual(stream.read(), fq('one') + fq('two'))

    def test_one_part_with_inconsistent_counters_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = config(root)
            cfg.update(unclassified='keep', min_qscore=0, shards=[['a'], ['b']])
            parts = []
            for index in range(2):
                native = root / f'tmp/native_{index}'
                path = native / 'fastq_pass/sample_A/calls.fastq'
                path.parent.mkdir(parents=True)
                path.write_bytes(fq(str(index)))
                log = root / f'b{index}.log'
                log.write_text(f'Simplex reads basecalled: 1\n> {1 + index} reads demuxed @ classifications/s: 1\n')
                parts.append((native, log, 1))
            with self.assertRaisesRegex(ValueError, 'demultiplexed count'):
                export_parts(root, cfg, parts)
