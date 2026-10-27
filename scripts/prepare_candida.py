#!/usr/bin/env python3
"""Prepare the specific Plate 3 FAST run without reading or copying signal arrays."""
import csv
import json
import re
import sys
from pathlib import Path

import pod5

root = Path(sys.argv[1]).resolve(strict=True)
source = Path('/ssd_workspace/gbrach/candida_pod5/Candida_plate3').resolve(strict=True)
assert root.is_relative_to('/ssd_workspace/gbrach')
files = sorted(source.rglob('*.pod5'))
assert files, 'No POD5 files'
manifest = []
metadata = set()
for path in files:
    path = path.resolve(strict=True)
    assert path.is_relative_to(source), 'Input resolves outside the source folder'
    stat = path.stat()
    with pod5.Reader(path) as reader:
        count = reader.num_reads
        assert count, f'Empty POD5: {path}'
        info = next(reader.reads()).run_info
        fields = (info.experiment_name, info.flow_cell_id, info.sequencing_kit.upper(),
                  info.flow_cell_product_code, info.sample_rate)
        metadata.add(fields)
    manifest.append({'path': str(path), 'bytes': stat.st_size,
                     'mtime_ns': stat.st_mtime_ns, 'reads': count})
assert len(metadata) == 1, f'Mixed runs or chemistries: {metadata}'
experiment, flowcell, kit, product, sample_rate = metadata.pop()
assert (experiment, flowcell, kit, product, sample_rate) == (
    'Candida_Plate3', 'PBM22663', 'SQK-NBD114-96', 'FLO-PRO114M', 5000)
with (root / 'config/aliases.tsv').open(newline='') as stream:
    rows = list(csv.DictReader(stream, delimiter='\t'))
assert len(rows) == 96 and len({r['barcode'] for r in rows}) == 96
assert len({r['alias'] for r in rows}) == 96
for row in rows:
    assert re.fullmatch(r'barcode(?:0[1-9]|[1-8][0-9]|9[0-6])', row['barcode'])
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,40}', row['alias'])
with (root / 'config/sample_sheet.csv').open('w', newline='') as stream:
    writer = csv.writer(stream)
    writer.writerow(['kit', 'experiment_id', 'flow_cell_id', 'barcode', 'alias'])
    writer.writerows((kit, experiment, flowcell, r['barcode'], r['alias']) for r in rows)
settings = {'input': str(source), 'input_files': len(files),
            'input_bytes': sum(r['bytes'] for r in manifest),
            'input_reads': sum(r['reads'] for r in manifest),
            'experiment': experiment, 'flowcell': flowcell, 'kit': kit,
            'sample_rate': sample_rate, 'model_tier': 'fast', 'gpus': 2,
            'min_qscore': 0, 'unclassified': 'keep', 'output': 'fastq.gz',
            'pod5_export': False, 'dorado_module': 'dorado/2.1.0'}
(root / 'config/input-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
(root / 'config/settings.json').write_text(json.dumps(settings, indent=2) + '\n')
print(json.dumps(settings, indent=2))
