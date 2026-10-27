#!/usr/bin/env python3
"""Stream-check native demultiplexed FASTQ.gz outputs using bounded memory."""
import argparse
import csv
import gzip
import json
import re
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path


def check_file(path):
    reads = bases = 0
    with gzip.open(path, 'rb') as stream:
        while True:
            header = stream.readline()
            if not header:
                break
            sequence = stream.readline().rstrip(b'\r\n')
            separator = stream.readline()
            quality_line = stream.readline()
            quality = quality_line.rstrip(b'\r\n')
            if (not header.startswith(b'@') or not separator.startswith(b'+')
                    or not quality_line.endswith(b'\n') or len(sequence) != len(quality)):
                raise ValueError(f'Malformed FASTQ record in {path}, record {reads + 1}')
            reads += 1
            bases += len(sequence)
    return {'path': str(path), 'alias': path.parent.name, 'reads': reads, 'bases': bases,
            'compressed_bytes': path.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('output_dir', type=Path)
    parser.add_argument('basecaller_log', type=Path)
    parser.add_argument('--minimum-reads', required=True, type=int)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--flat', action='store_true', help='Read output_dir/fastq/ALIAS.fastq.gz')
    args = parser.parse_args()
    with (args.run_dir / 'config/aliases.tsv').open(newline='') as stream:
        aliases = {row['alias'] for row in csv.DictReader(stream, delimiter='\t')}
    aliases.add('unclassified')
    files = sorted((args.output_dir / 'fastq').glob('*.fastq.gz')
                   if args.flat else args.output_dir.rglob('*.fastq.gz'))
    if not files:
        raise ValueError('No FASTQ.gz outputs found')
    for path in files:
        alias = path.name[:-len('.fastq.gz')] if args.flat else path.parent.name
        if alias not in aliases:
            raise ValueError(f'Unexpected sample name: {alias}')
        if not args.flat and ('fastq_pass' not in path.parts or 'fastq_fail' in path.parts):
            raise ValueError(f'Unexpected output group with Q0: {path}')
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        records = list(pool.map(check_file, files))
    if args.flat:
        for row in records:
            row['alias'] = Path(row['path']).name[:-len('.fastq.gz')]
    count = sum(row['reads'] for row in records)
    log = args.basecaller_log.read_text()
    raw_matches = re.findall(r'Simplex reads basecalled: ([0-9]+)', log)
    demux_matches = re.findall(r'> ([0-9]+) reads demuxed @', log)
    if len(demux_matches) != 1 or count != int(demux_matches[0]):
        raise ValueError(f'FASTQ count {count} does not match Dorado demultiplexed count {demux_matches}')
    if len(raw_matches) != 1 or int(raw_matches[0]) != args.minimum_reads:
        raise ValueError(f'Expected {args.minimum_reads} raw reads basecalled; Dorado reports {raw_matches}')
    counts, bases = Counter(), Counter()
    for row in records:
        counts[row['alias']] += row['reads']
        bases[row['alias']] += row['bases']
    with (args.output_dir / 'sample_counts.tsv').open('w', newline='') as stream:
        writer = csv.writer(stream, delimiter='\t')
        writer.writerow(['alias', 'reads', 'bases'])
        writer.writerows((alias, counts[alias], bases[alias]) for alias in sorted(aliases))
    report = {'verification': 'passed', 'checks': ['gzip CRC', 'FASTQ record structure',
              'sequence/quality lengths', 'known sample names', 'Dorado completion count'],
              'fastq_files': len(files), 'raw_reads_basecalled': int(raw_matches[0]),
              'reads': count, 'bases': sum(bases.values()),
              'unclassified_reads': counts['unclassified'], 'files': records}
    (args.output_dir / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'Verified {count:,} reads in {len(files)} FASTQ.gz files; '
          f'{counts["unclassified"]:,} unclassified reads retained.', flush=True)


if __name__ == '__main__':
    main()
