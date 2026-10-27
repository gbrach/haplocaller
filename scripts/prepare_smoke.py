#!/usr/bin/env python3
"""Prepare the small Pangloss validation run; does not modify source POD5s."""
import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

import pod5


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    root = args.run_dir.resolve(strict=True)
    if not root.is_relative_to(Path("/ssd_workspace/gbrach")):
        raise ValueError("Pangloss runs must be under /ssd_workspace/gbrach")
    metadata = {}
    records = []
    seen = set()
    checksums = {}
    for path in sorted((root / "input/pod5").glob("*.pod5")):
        checksums[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        with pod5.Reader(path) as reader:
            for record in reader.reads():
                read_id = str(record.read_id)
                if read_id in seen:
                    raise ValueError("Duplicate raw read ID: " + read_id)
                seen.add(read_id)
                info = record.run_info
                kit = info.sequencing_kit.upper()
                if kit != "SQK-NBD114-96":
                    raise ValueError("This test expects SQK-NBD114-96, found " + kit)
                for value in (info.experiment_name, info.flow_cell_id):
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
                        raise ValueError("Unsupported sample-sheet metadata: " + value)
                key = (info.experiment_name, info.flow_cell_id)
                metadata[key] = kit
                records.append((read_id, path.name))
    if not records:
        raise ValueError("No input reads found")
    with (root / "input/manifest.json").open() as stream:
        manifest = json.load(stream)
    expected_count = sum(source["raw_read_count"] for source in manifest["sources"])
    if len(records) != expected_count:
        raise ValueError("Input read count does not match the subset manifest")
    with (root / "input/test_aliases.tsv").open(newline="") as stream:
        aliases = list(csv.DictReader(stream, delimiter="\t"))
    if not aliases or len({row["barcode"] for row in aliases}) != len(aliases):
        raise ValueError("Missing or duplicated barcode rows")
    if len({row["alias"] for row in aliases}) != len(aliases):
        raise ValueError("This test requires unique aliases")
    for row in aliases:
        if not re.fullmatch(r"barcode(?:0[1-9]|[1-8][0-9]|9[0-6])", row["barcode"]):
            raise ValueError("Invalid barcode: " + row["barcode"])
        if not re.fullmatch(r"test_barcode[0-9]{2}", row["alias"]):
            raise ValueError("The smoke test uses synthetic test_barcodeNN aliases")
    with (root / "input/sample_sheet.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["kit", "experiment_id", "flow_cell_id", "barcode", "alias"])
        for (experiment, flowcell), kit in sorted(metadata.items()):
            for row in aliases:
                writer.writerow([kit, experiment, flowcell, row["barcode"], row["alias"]])
    with (root / "input/raw_reads.tsv").open("w", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["read_id", "source_file"])
        writer.writerows(records)
    settings = {
        "raw_read_count": len(records),
        "kit": "SQK-NBD114-96",
        "model_tier": "hac",
        "min_qscore": 10,
        "unclassified": "keep",
        "below_threshold": "save_separately",
        "archive_quality_policy": "all_raw_reads",
        "archive_conflict_policy": "any_disagreeing_children_go_to_ambiguous",
        "input_sha256": checksums,
        "synthetic_aliases": True,
        "pod5_version": pod5.__version__,
    }
    (root / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    print("Prepared %d raw reads and %d synthetic barcode aliases" % (len(records), len(aliases)))


if __name__ == "__main__":
    main()
