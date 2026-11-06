import gzip
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from haplocaller.common import native_format, outputs
from haplocaller.worker import export_results

try:
    import pod5
    import pysam
except ImportError:  # the cluster environment has both; a plain laptop may not
    pod5 = pysam = None

SMOKE = Path(__file__).resolve().parents[1] / 'testdata/pangloss-smoke/pod5'


def base_config(root, **extra):
    config = {'name': 'run', 'run_dir': str(root), 'mode': 'full', 'tier': 'fast',
              'profile': {'name': 't', 'cpus': 2, 'minimum_free_gib': 0},
              'probe': {'files': [], 'reads': 0}, 'mapping': [{'barcode': 'barcode01', 'alias': 'sample_A'}],
              'output': 'fastq', 'unclassified': 'keep', 'min_qscore': 7, 'model_name': 'm',
              'software': {'version': '2.1.0'}}
    config.update(extra)
    return config


class FormatSelectionTests(unittest.TestCase):
    def test_old_configs_still_read_as_one_format(self):
        self.assertEqual(outputs({'output': 'bam'}), ['bam'])
        self.assertEqual(native_format({'output': 'fastq'}), 'fastq')
        self.assertEqual(native_format({'output': 'bam'}), 'bam')

    def test_dorado_writes_bam_when_anything_needs_its_tags(self):
        self.assertEqual(native_format({'outputs': ['fastq']}), 'fastq')
        self.assertEqual(native_format({'outputs': ['fastq'], 'pod5': True}), 'bam')
        self.assertEqual(native_format({'outputs': ['fastq', 'bam']}), 'bam')


@unittest.skipUnless(pysam and pod5 and SMOKE.is_dir(), 'needs pysam, pod5 and the local smoke POD5s')
class BothFormatsAndPod5Tests(unittest.TestCase):
    def make(self, root, pilot=False):
        files, ids = [], []
        for path in sorted(SMOKE.glob('*.pod5')):
            with pod5.Reader(path) as reader:
                found = [str(r.read_id) for r in reader.reads()]
            files.append({'path': str(path), 'reads': len(found)})
            ids += found
        header = {'HD': {'VN': '1.6', 'SO': 'unknown'}, 'SQ': [], 'RG': [{'ID': 'rg1', 'SM': 'x'}]}
        native = root / 'tmp/native'
        plan = {('bam_pass', 'sample_A'): ids[:40], ('bam_fail', 'sample_A'): ids[40:50],
                ('bam_pass', 'unclassified'): ids[50:60]}
        # ids[0] also has a child in unclassified, so it is "ambiguous"; ids[60:] have no calls at all
        plan[('bam_pass', 'unclassified')] = ids[50:60] + [ids[0]]
        for (group, alias), members in plan.items():
            path = native / group / alias / 'calls.bam'
            path.parent.mkdir(parents=True)
            with pysam.AlignmentFile(str(path), 'wb', header=header) as out:
                for number, read_id in enumerate(members):
                    child = str(uuid.uuid4()) if read_id == ids[0] and alias == 'unclassified' else read_id
                    record = pysam.AlignedSegment(out.header)
                    record.query_name = child
                    record.query_sequence = 'ACGTACGT'
                    record.query_qualities = pysam.qualitystring_to_array('IIIIIIII')
                    record.flag = 4
                    record.set_tag('pi', read_id)
                    out.write(record)
        log = root / 'basecaller.log'
        total = sum(len(v) for v in plan.values())
        log.write_text(f'Simplex reads basecalled: {total}\n> {total} reads demuxed @ classifications/s: 3\n')
        config = base_config(root, outputs=['fastq', 'bam'], pod5=True, mode='pilot' if pilot else 'full',
                             probe={'files': files, 'reads': len(ids)})
        return config, native, log, ids, total

    def test_fastq_and_bam_have_the_same_reads_and_pod5_covers_every_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, native, log, ids, total = self.make(root)
            report = export_results(root, config, native, log, total)
            self.assertEqual(report['pod5']['status'], 'passed', report['pod5'])
            self.assertEqual(report['pod5']['reads'], len(ids))
            self.assertEqual(report['kept_reads'], 51)  # 40 in sample_A + 11 unclassified; 10 failed Q
            with gzip.open(root / 'fastq/sample_A.fastq.gz', 'rt') as stream:
                fastq_names = [line[1:].split('\t')[0].strip() for i, line in enumerate(stream) if i % 4 == 0]
            with pysam.AlignmentFile(str(root / 'bam/sample_A.bam'), 'rb', check_sq=False) as stream:
                bam_names = [r.query_name for r in stream]
            self.assertEqual(fastq_names, bam_names)
            self.assertEqual(len(bam_names), 40)
            counts = {p.name: sum(1 for _ in pod5.Reader(p).reads()) for p in (root / 'pod5').glob('*.pod5')}
            self.assertEqual(counts['sample_A.pod5'], 49)       # includes the 10 low-quality reads
            self.assertEqual(counts['_ambiguous.pod5'], 1)
            self.assertEqual(counts['unclassified.pod5'], 10)
            self.assertEqual(counts['_uncalled.pod5'], len(ids) - 60)
            self.assertFalse(list((root / 'pod5').glob('*.partial')))
            self.assertEqual(json.loads((root / 'logs/verification.json').read_text())['pod5']['status'], 'passed')

    def test_clean_keeps_both_formats_and_the_pod5_archive(self):
        from haplocaller.cleanup import cleanup_run
        from haplocaller.common import run_file, write_json
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, native, log, ids, total = self.make(root)
            (root / 'config').mkdir()
            write_json(root / 'config/run.json', config)
            export_results(root, config, native, log, total)
            write_json(run_file(root, 'status.json'), {'state': 'complete'})
            cleanup_run(root, check_scheduler=False)
            for kept in ('fastq', 'bam', 'pod5', 'logs', 'config'):
                self.assertTrue((root / kept).is_dir(), kept)
            self.assertTrue((root / 'pod5/sample_A.pod5').is_file())
            self.assertFalse((root / 'tmp').exists())

    def test_fastq_header_keeps_the_tags_shared_with_dorados_own(self):
        header = pysam.AlignmentHeader.from_dict({'HD': {'VN': '1.6'}, 'SQ': []})
        record = pysam.AlignedSegment(header)
        record.query_name = 'abc'
        record.query_sequence = 'ACGT'
        record.query_qualities = pysam.qualitystring_to_array('IIII')
        record.set_tags([('qs', 10.6263, 'f'), ('ch', 870, 'i'), ('st', '2026-08-28T11:52:52.499000+00:00', 'Z'), ('mx', 3, 'i'), ('RG', 'run_model', 'Z')])
        from haplocaller.worker import fastq_text
        self.assertEqual(fastq_text(record).decode(),
                         '@abc\tqs:f:10.6263\tch:i:870\tst:Z:2026-08-28T11:52:52.499000+00:00\tRG:Z:run_model\nACGT\n+\nIIII\n')

    def test_pilot_archives_only_basecalled_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, native, log, ids, total = self.make(root, pilot=True)
            report = export_results(root, config, native, log, total)
            self.assertEqual(report['pod5']['reads'], 60)
            self.assertFalse((root / 'pod5/_uncalled.pod5').exists())

    def test_pod5_failure_keeps_the_verified_basecalls(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, native, log, ids, total = self.make(root)
            with patch('haplocaller.worker.raw_signature', side_effect=RuntimeError('disk full')):
                report = export_results(root, config, native, log, total)
            self.assertEqual(report['pod5']['status'], 'failed')
            self.assertFalse((root / 'pod5').exists())
            self.assertTrue((root / 'fastq/sample_A.fastq.gz').is_file())
            self.assertTrue((root / 'bam/sample_A.bam').is_file())
            self.assertEqual(report['verification'], 'passed')


if __name__ == '__main__':
    unittest.main()
