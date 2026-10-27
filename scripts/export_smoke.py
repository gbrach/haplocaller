#!/usr/bin/env python3
"""Export and independently check all three output types for the small test set.

This deliberately retains all calls (pass/fail separately) and all raw signals.
It is a bounded smoke test, not the production streaming/export implementation.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
import csv
import gzip
import hashlib
import json
import math
import os
from pathlib import Path

import pod5
import pysam


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def quality_bucket(path):
    buckets = [value[4:] for value in path.parts if value in ("bam_pass", "bam_fail")]
    require(len(buckets) == 1, "Expected Dorado bam_pass/bam_fail directory: " + str(path))
    return buckets[0]


def raw_signature(record):
    return (
        hashlib.sha256(record.signal.tobytes()).hexdigest(),
        record.num_samples, record.read_number, record.start_sample,
        record.pore, record.calibration, record.end_reason,
        "NaN" if math.isnan(record.median_before) else record.median_before, record.run_info,
    )


def combined_header(paths):
    groups = {"RG": {}, "PG": {}}
    comments = set()
    for path in paths:
        with pysam.AlignmentFile(str(path), "rb", check_sq=False) as bam:
            bam.check_truncation()
            header = bam.header.to_dict()
            require(not header.get("SQ"), "This test expects unaligned basecalls")
            for kind in groups:
                for item in header.get(kind, []):
                    previous = groups[kind].get(item["ID"])
                    require(previous is None or previous == item, "Conflicting BAM header ID")
                    groups[kind][item["ID"]] = item
            comments.update(header.get("CO", []))
    result = {"HD": {"VN": "1.6", "SO": "unknown"}, "SQ": []}
    result.update({kind: list(items.values()) for kind, items in groups.items() if items})
    if comments:
        result["CO"] = sorted(comments)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    root = args.run_dir.resolve(strict=True)
    require(root.is_relative_to(Path("/ssd_workspace/gbrach")), "Run must use Pangloss SSD workspace")
    settings = json.loads((root / "settings.json").read_text())
    results = root / "results"
    results.mkdir(exist_ok=True)
    for name in ("bam", "fastq", "pod5"):
        require(not (results / name).exists(), "Refusing to overwrite existing export: " + name)
        (results / name).mkdir()

    sources = sorted((root / "input/pod5").glob("*.pod5"))
    raw_original = {}
    for path in sources:
        require(hashlib.sha256(path.read_bytes()).hexdigest() == settings["input_sha256"][path.name],
                "Staged input changed: " + str(path))
        with pod5.Reader(path) as reader:
            for record in reader.reads():
                read_id = str(record.read_id)
                require(read_id not in raw_original, "Duplicate raw input ID")
                raw_original[read_id] = raw_signature(record)
    require(len(raw_original) == settings["raw_read_count"], "Raw input count changed")

    with (root / "input/test_aliases.tsv").open(newline="") as stream:
        aliases = {row["alias"] for row in csv.DictReader(stream, delimiter="\t")}
    bam_files = sorted((root / "calls").rglob("*.bam"))
    require(bool(bam_files), "Dorado produced no BAM files")
    header = combined_header(bam_files)
    records = {}
    parent_assignments = defaultdict(set)
    parent_child_counts = Counter()
    counts = Counter()
    qscore_ranges = {}
    split_count = 0
    with ExitStack() as stack:
        writers = {}
        for path in bam_files:
            quality = quality_bucket(path)
            with pysam.AlignmentFile(str(path), "rb", check_sq=False) as bam:
                for record in bam.fetch(until_eof=True):
                    require(not record.is_secondary and not record.is_supplementary,
                            "Unexpected non-primary record in basecalls")
                    read_id = record.query_name
                    require(read_id not in records, "Duplicate basecalled read ID: " + read_id)
                    parent = record.get_tag("pi") if record.has_tag("pi") else read_id
                    require(parent in raw_original, "Basecalled read has no input parent: " + read_id)
                    split_count += int(parent != read_id)
                    alias = record.get_tag("BC") if record.has_tag("BC") else "unclassified"
                    require(alias == "unclassified" or alias in aliases,
                            "Expected sample alias in BC tag, found: " + str(alias))
                    require(record.query_sequence is not None and record.qual is not None,
                            "Missing sequence or quality in " + read_id)
                    require(record.has_tag("qs"), "Missing Dorado mean-Q tag")
                    qscore, tag_type = record.get_tag("qs", with_value_type=True)
                    qscore = float(qscore)
                    tolerance = 0.001 if tag_type in ("f", "d") else 0.5001
                    threshold = settings["min_qscore"]
                    require((quality == "pass" and qscore >= threshold - tolerance) or
                            (quality == "fail" and qscore <= threshold + tolerance),
                            "Dorado pass/fail bucket disagrees with its Q-score tag")
                    bounds = qscore_ranges.setdefault(quality, [qscore, qscore])
                    bounds[0], bounds[1] = min(bounds[0], qscore), max(bounds[1], qscore)
                    key = (quality, alias)
                    if key not in writers:
                        for kind in ("bam", "fastq"):
                            (results / kind / quality).mkdir(exist_ok=True)
                        out_bam = stack.enter_context(pysam.AlignmentFile(
                            str(results / "bam" / quality / (alias + ".bam")), "wb", header=header))
                        out_fastq = stack.enter_context(gzip.open(
                            results / "fastq" / quality / (alias + ".fastq.gz"), "wt"))
                        writers[key] = out_bam, out_fastq
                    out_bam, out_fastq = writers[key]
                    out_bam.write(record)
                    out_fastq.write("@%s\n%s\n+\n%s\n" % (read_id, record.query_sequence, record.qual))
                    records[read_id] = (quality, alias, parent, record.query_sequence, record.qual)
                    parent_assignments[parent].add(alias)
                    parent_child_counts[parent] += 1
                    counts[key] += 1
    require(bool(records), "Dorado produced no basecalled reads")

    destinations = {}
    for parent in raw_original:
        choices = parent_assignments[parent]
        if not choices:
            destinations[parent] = "archive_uncalled"
        elif len(choices) == 1:
            destinations[parent] = next(iter(choices))
        else:
            destinations[parent] = "archive_ambiguous"
    archive_counts = Counter(destinations.values())
    with ExitStack() as stack:
        writers = {}
        for path in sources:
            with pod5.Reader(path) as reader:
                for record in reader.reads():
                    destination = destinations[str(record.read_id)]
                    if destination not in writers:
                        writers[destination] = stack.enter_context(
                            pod5.Writer(results / "pod5" / (destination + ".pod5")))
                    writers[destination].add_read(record.to_read())

    verified_bam = set()
    verified_fastq = set()
    verified_raw = set()
    for path in sorted((results / "bam").rglob("*.bam")):
        with pysam.AlignmentFile(str(path), "rb", check_sq=False) as bam:
            bam.check_truncation()
            for record in bam.fetch(until_eof=True):
                read_id = record.query_name
                require(read_id not in verified_bam, "Duplicate exported BAM record")
                verified_bam.add(read_id)
                quality, alias, parent, sequence, qual = records[read_id]
                require(path.parent.name == quality and path.stem == alias, "BAM routing mismatch")
                require((record.query_sequence, record.qual) == (sequence, qual), "BAM sequence changed")
                require((record.get_tag("pi") if record.has_tag("pi") else read_id) == parent,
                        "BAM parent association changed")
    for path in sorted((results / "fastq").rglob("*.fastq.gz")):
        with gzip.open(path, "rt") as stream:
            while True:
                name = stream.readline()
                if not name:
                    break
                sequence = stream.readline().rstrip("\n")
                separator = stream.readline().rstrip("\n")
                qual = stream.readline().rstrip("\n")
                require(name.startswith("@") and separator == "+", "Malformed FASTQ record")
                read_id = name[1:].rstrip("\n")
                require(read_id not in verified_fastq, "Duplicate exported FASTQ record")
                verified_fastq.add(read_id)
                quality, alias, _, expected_sequence, expected_qual = records[read_id]
                require(path.parent.name == quality and path.name == alias + ".fastq.gz", "FASTQ routing mismatch")
                require((sequence, qual) == (expected_sequence, expected_qual), "FASTQ sequence changed")
    for path in sorted((results / "pod5").glob("*.pod5")):
        with pod5.Reader(path) as reader:
            for record in reader.reads():
                parent = str(record.read_id)
                require(parent not in verified_raw, "Raw signal exported more than once")
                verified_raw.add(parent)
                require(path.stem == destinations[parent], "Raw signal routing mismatch")
                require(raw_signature(record) == raw_original[parent], "Raw signal or metadata changed")
    require(verified_bam == set(records) == verified_fastq, "BAM/FASTQ read IDs do not match")
    require(verified_raw == set(raw_original), "Raw-signal archive does not cover every input")

    with (results / "raw_assignments.tsv").open("w", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["raw_read_id", "archive_group", "basecalled_children", "child_assignments"])
        for parent in sorted(destinations):
            writer.writerow([parent, destinations[parent], parent_child_counts[parent],
                             ",".join(sorted(parent_assignments[parent]))])
    with (results / "sample_counts.tsv").open("w", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["alias", "passing_basecalls", "failing_basecalls", "archived_raw_reads"])
        all_aliases = {alias for _, alias in counts} | set(archive_counts)
        for alias in sorted(all_aliases):
            writer.writerow([alias, counts["pass", alias], counts["fail", alias], archive_counts[alias]])
    passing = sum(count for (quality, _), count in counts.items() if quality == "pass")
    failing = len(records) - passing
    unclassified = sum(count for (_, alias), count in counts.items() if alias == "unclassified")
    summary = {
        "raw_input_reads": len(raw_original), "basecalled_records": len(records),
        "split_child_records": split_count,
        "raw_parents_with_basecalls": sum(bool(choices) for choices in parent_assignments.values()),
        "passing_basecalls": passing, "failing_basecalls": failing,
        "unclassified_basecalls": unclassified, "qscore_ranges": qscore_ranges,
        "raw_archive_counts": dict(sorted(archive_counts.items())),
        "raw_signals_verified": len(verified_raw), "bam_records_verified": len(verified_bam),
        "fastq_records_verified": len(verified_fastq),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"), "host": os.uname().nodename,
        "model": Path((root / "model.txt").read_text().strip()).name,
        "dorado_version_output": (root / "details/dorado-version.txt").read_text().strip(),
        "basecalling_seconds": int((root / "details/basecalling-seconds.txt").read_text()),
        "settings": settings,
        "verification": "passed",
    }
    (results / "verification.json").write_text(json.dumps(summary, indent=2) + "\n")
    text = (
        "Raw input reads: %d\nBasecalled records: %d (%d split children)\n"
        "Passing Q10: %d; below Q10: %d (saved separately)\n"
        "Unclassified basecalls: %d (kept; overlaps the quality counts)\n"
        "Archived raw signals: %d; ambiguous: %d; without basecalls: %d\n"
        "Verification passed: BAM/FASTQ IDs and sequences match; all raw signals and metadata preserved.\n"
        "Aliases are synthetic test labels.\n"
    ) % (len(raw_original), len(records), split_count, passing, failing, unclassified,
         len(verified_raw), archive_counts["archive_ambiguous"], archive_counts["archive_uncalled"])
    (results / "summary.txt").write_text(text)
    with (results / "checksums.sha256").open("w") as stream:
        for path in sorted(results.rglob("*")):
            if path.is_file() and path.name != "checksums.sha256":
                stream.write(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + str(path.relative_to(results)) + "\n")
    print(text, end="")


if __name__ == "__main__":
    main()
