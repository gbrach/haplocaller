"""Accounting for unaligned simplex output produced with Dorado --output-dir."""
import csv
import re
import tempfile
from pathlib import Path

from .common import run_file, timestamp, write_json

COUNT_FIELDS = ['alias', 'reads', 'bases', 'discarded_quality',
                'discarded_unclassified', 'native_records']


def dorado_counts(text, expected_raw, written):
    def counter(pattern, name, optional=False):
        values = re.findall(pattern, text)
        if not values and optional:
            return 0
        if len(values) != 1:
            raise ValueError(f'Expected one Dorado {name} counter; found {values}.')
        return int(values[0])

    # Dorado omits these two counters when they are zero. There can still be
    # written later segments when every first segment was filtered.
    primary = counter(r'Simplex reads basecalled: ([0-9]+)', 'basecalled', optional=True)
    filtered = counter(r'Simplex reads filtered: ([0-9]+)', 'filtered', optional=True)
    demux = counter(r'> ([0-9]+) reads demuxed @', 'demultiplexed')
    # Barcode classification precedes ReadFilterNode. With --output-dir, Q-fail
    # records are written; this filter removes short sequences after trimming.
    if written + filtered != demux:
        raise ValueError(f'Dorado demultiplexed count {demux:,} does not equal '
                         f'{written:,} written + {filtered:,} internally filtered records.')
    # "Simplex reads basecalled" counts written subread_id=0 only. Filtering
    # may remove first segments, additional split segments, or both. Adding all
    # filtered segments to that counter does NOT give an exact raw-read count.
    if not (0 <= primary <= expected_raw <= primary + filtered and primary <= written):
        raise ValueError(f'Input count {expected_raw:,} is inconsistent with Dorado: '
                         f'{primary:,} first segments written, {filtered:,} segments filtered, '
                         f'{written:,} records written.')
    return {
        'input_raw_reads': expected_raw,
        'dorado_primary_reads_written': primary,
        'dorado_filtered_records': filtered,
        'demultiplexed_records': demux,
        'basecall_records': written,
        'raw_reads_without_primary_output': expected_raw - primary,
        'filtered_nonprimary_records': filtered - (expected_raw - primary),
        'additional_split_records': demux - expected_raw,
    }


def combine_counters(parts):
    """Add the per-part counters of a sharded run; each part was verified on its own."""
    return {key: sum(part[key] for part in parts) for key in parts[0]}


def write_report(root, config, rows, counters, recovery=None):
    by_alias = {row['alias']: row for row in rows}
    with tempfile.NamedTemporaryFile(mode='w', dir=root, prefix='sample_counts.',
                                     suffix='.partial', newline='', delete=False) as stream:
        temporary = Path(stream.name)
        writer = csv.DictWriter(stream, fieldnames=COUNT_FIELDS, delimiter='\t', extrasaction='ignore')
        writer.writeheader()
        for alias in sorted({row['alias'] for row in config['mapping']} | {'unclassified'}):
            writer.writerow(by_alias.get(alias, dict(alias=alias, **{k: 0 for k in COUNT_FIELDS[1:]})))
    temporary.replace(run_file(root, 'sample_counts.tsv'))
    report = dict(counters, schema_version=2, verification='passed',
                  kept_reads=sum(row['reads'] for row in rows),
                  discarded_quality=sum(row['discarded_quality'] for row in rows),
                  discarded_unclassified=sum(row['discarded_unclassified'] for row in rows),
                  mode=config['mode'], model=config['model_name'],
                  dorado=config['software']['version'], completed_at=timestamp(),
                  checks=['complete output streams', 'record structure', 'sample names',
                          'per-sample exported counts',
                          'demultiplexed = written + internally filtered records',
                          'raw input count within first-segment/filter bounds'],
                  limits='Input read-ID uniqueness and complete raw-parent coverage are not proven '
                         'by aggregate counters; Dorado does not list internally filtered read IDs.')
    if recovery is not None:
        report['recovery'] = recovery
        report['checks'] += ['per-sample read ID/length multiset matches sequencing summary',
                             'input manifest unchanged', 'output files unchanged during validation']
    write_json(run_file(root, 'verification.json'), report)
    return report


def revalidated(report, job_id):
    recovery = report.get('recovery', {})
    return (report.get('verification') == 'passed'
            and recovery.get('method') == 'existing_outputs'
            and job_id is not None
            and str(recovery.get('original_job_id')) == str(job_id))
