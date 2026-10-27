#!/usr/bin/env python3
"""Merge native FASTQ batches into one gzip file per alias; retain source files."""
import argparse
import gzip
import re
import shutil
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('native_dir', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--workers', type=int, default=8)
    args = parser.parse_args()
    groups = defaultdict(list)
    for path in sorted(args.native_dir.rglob('*')):
        if not path.is_file() or not (path.name.endswith('.fastq') or path.name.endswith('.fastq.gz')):
            continue
        alias = path.parent.name
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', alias):
            raise ValueError(f'Invalid output alias: {alias}')
        if 'fastq_fail' in path.parts or 'fastq_pass' not in path.parts:
            raise ValueError(f'Unexpected Q0 output group: {path}')
        groups[alias].append(path)
    if not groups:
        raise ValueError('No native FASTQ outputs found')
    args.destination.mkdir(parents=True, exist_ok=False)

    def pack(item):
        alias, sources = item
        output = args.destination / (alias + '.fastq.gz')
        partial = output.with_name(output.name + '.partial')
        with partial.open('xb') as raw:
            with gzip.GzipFile(filename='', fileobj=raw, mode='wb', compresslevel=1, mtime=0) as out:
                for source in sources:
                    opener = gzip.open if source.name.endswith('.gz') else open
                    with opener(source, 'rb') as stream:
                        shutil.copyfileobj(stream, out, length=1024 * 1024)
        partial.rename(output)
        return alias

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        completed = list(pool.map(pack, groups.items()))
    print(f'Created {len(completed)} per-sample FASTQ.gz files in {args.destination}', flush=True)


if __name__ == '__main__':
    main()
