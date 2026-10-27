"""Read POD5 metadata with the cluster's configured Python, without loading signals."""
import json
import sys
from pathlib import Path


def probe(source):
    try:
        import pod5
    except ImportError:
        raise RuntimeError('The cluster Python needs the pod5 package. Ask the profile maintainer to configure it once.')
    source = Path(source).expanduser().resolve(strict=True)
    files = [source] if source.is_file() and source.suffix.lower() == '.pod5' else sorted(source.rglob('*.pod5'))
    if not files:
        raise RuntimeError('No POD5 files found in that input.')
    if len({path.resolve() for path in files}) != len(files):
        raise RuntimeError('The same POD5 file is linked more than once in this input.')
    manifest, metadata = [], set()
    for index, path in enumerate(files, 1):
        path = path.resolve(strict=True)
        stat = path.stat()
        with pod5.Reader(path) as reader:
            count = reader.num_reads
            if count:
                info = next(reader.reads()).run_info
                metadata.add((info.experiment_name, info.flow_cell_id, info.sequencing_kit.upper(),
                              info.flow_cell_product_code, info.sample_rate))
        manifest.append({'path': str(path), 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'reads': count})
        if len(files) > 20 and (index % 20 == 0 or index == len(files)):
            print(f'  Checked {index}/{len(files)} POD5 files', file=sys.stderr, flush=True)
    if not metadata:
        raise RuntimeError('All input POD5 files are empty.')
    if len(metadata) != 1:
        raise RuntimeError('This input contains different runs or chemistries. Select one sequencing run per submission.')
    experiment, flowcell, kit, product, sample_rate = metadata.pop()
    return {'input': str(source), 'files': manifest, 'reads': sum(row['reads'] for row in manifest),
            'bytes': sum(row['bytes'] for row in manifest), 'experiment': experiment,
            'flowcell': flowcell, 'kit': kit, 'product': product, 'sample_rate': sample_rate,
            'metadata_check': 'first read of each nonempty POD5 file', 'pod5_version': pod5.__version__}


if __name__ == '__main__':
    try:
        print(json.dumps(probe(sys.argv[1])))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
