#!/usr/bin/env python3
"""Recover a finalized-but-unpublished FlightIndex into a new SQLite file.

Uses read-only source access and the builder's advisory lock. Never rebuilds
archives or replaces the source. Run on Linux/WSL; all output must be new.
"""
import argparse
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time


def identity(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def recover(source, destination, max_runtime_s=600):
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if source.suffix != ".building" or not source.is_file():
        raise ValueError("Source must be an existing, explicitly selected .building file")
    if destination.suffix != ".sqlite" or destination.exists():
        raise ValueError("Destination must be a new .sqlite file")
    partial = destination.with_suffix(".sqlite.recovering")
    if partial.exists():
        raise FileExistsError(f"Preserved partial output already exists: {partial}")
    began = time.monotonic()
    phase, last_report = "source_metadata", -math.inf

    def report(force=False):
        nonlocal last_report
        elapsed = time.monotonic() - began
        if force or elapsed - last_report >= 5:
            print(json.dumps({"phase": phase, "elapsed_s": round(elapsed, 1),
                              "output_bytes": partial.stat().st_size if partial.exists() else 0}),
                  flush=True)
            last_report = elapsed
        return int(elapsed >= max_runtime_s)

    def check_time():
        if report():
            raise TimeoutError("Recovery budget exhausted; partial output preserved")

    # FlightIndex.build uses this exact lock before unlinking its .building.
    # Do not create/truncate the old lock or change the old cache.
    with source.with_suffix(".lock").open("rb") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = identity(source)
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1) as original:
            original.set_progress_handler(report, 100000)
            metadata_rows = original.execute("SELECT value FROM metadata").fetchall()
            if len(metadata_rows) != 1:
                raise ValueError("Expected one committed metadata row")
            metadata_text = metadata_rows[0][0]
            metadata = json.loads(metadata_text)
            tables = {row[0] for row in original.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"segments", "spatial", "prepared_tracks", "metadata"}
            if (not required <= tables or "samples" in tables or
                    metadata.get("index_version") != 2 or
                    not metadata.get("prepared_tracks") or
                    metadata.get("segments", 0) <= 0):
                raise ValueError("Source did not finish segment/track preparation")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Reserve only our own empty output; VACUUM INTO accepts an empty file.
            with partial.open("xb"):
                pass
            phase = "vacuum_into_new_file"
            report(force=True)
            original.execute("VACUUM INTO ?", (str(partial),))
            check_time()
            phase = "validate_sqlite"
            report(force=True)
            with sqlite3.connect(partial.as_uri() + "?mode=ro", uri=True) as copied:
                copied.set_progress_handler(report, 100000)
                checks = [row[0] for row in copied.execute("PRAGMA integrity_check")]
                if checks != ["ok"]:
                    raise ValueError(f"SQLite integrity check failed: {checks[:10]}")
                if copied.execute("SELECT rtreecheck('spatial')").fetchone()[0] != "ok":
                    raise ValueError("Spatial R-tree failed its consistency check")
                if copied.execute("SELECT value FROM metadata").fetchall() != metadata_rows:
                    raise ValueError("Recovered metadata changed")
                counts = {table: copied.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                          for table in ("segments", "spatial", "prepared_tracks")}
                if (counts["segments"] != metadata["segments"] or
                        counts["spatial"] != counts["segments"] or
                        counts["prepared_tracks"] != metadata["prepared_tracks"]["tracks"]):
                    raise ValueError(f"Recovered counts disagree with metadata: {counts}")
                phase = "validate_recorded_payload_samples"
                report(force=True)
                first, last = copied.execute("SELECT MIN(id), MAX(id) FROM segments").fetchone()
                targets = sorted({first + (last - first) * i // 31 for i in range(32)})
                checked = 0
                for target in targets:
                    check_time()
                    row = copied.execute(
                        "SELECT id,name,payload,payload_hash FROM segments WHERE id>=? ORDER BY id LIMIT 1",
                        (target,)).fetchone()
                    if row != original.execute(
                            "SELECT id,name,payload,payload_hash FROM segments WHERE id=?",
                            (row[0],)).fetchone():
                        raise ValueError("Recorded segment changed while copying")
                    if hashlib.sha256(row[2]).hexdigest() != row[3]:
                        raise ValueError(f"Corrupt recorded payload: {row[1]}")
                    observations = json.loads(gzip.decompress(row[2]))
                    if not isinstance(observations, list) or len(observations) < 3:
                        raise ValueError("Invalid recorded segment observations")
                    checked += 1
                prepared_checked = 0
                pfirst, plast = copied.execute(
                    "SELECT MIN(id),MAX(id) FROM prepared_tracks").fetchone()
                for target in sorted({pfirst + (plast - pfirst) * i // 31 for i in range(32)}):
                    check_time()
                    row = copied.execute(
                        "SELECT p.id,p.source_hash,p.payload,p.payload_hash,s.payload_hash "
                        "FROM prepared_tracks p JOIN segments s ON s.id=p.id "
                        "WHERE p.id>=? ORDER BY p.id LIMIT 1", (target,)).fetchone()
                    if (row[1] != row[4] or hashlib.sha256(row[2]).hexdigest() != row[3]):
                        raise ValueError("Corrupt prepared representation")
                    prepared_checked += 1
            if identity(source) != before:
                raise ValueError("Source changed during recovery; output not published")
            check_time()
            # Hard-link creation is exclusive: never overwrite a concurrent result.
            # Source/output are different files; only the output's temporary name is removed.
            with partial.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(partial, destination)
            partial.unlink()
            summary = {"source": str(source), "destination": str(destination),
                       "source_unchanged": True, "sqlite_integrity": "ok", "rtree_check": "ok",
                       "counts": counts, "sampled_raw_payloads": checked,
                       "sampled_prepared_payloads": prepared_checked,
                       "source_bytes": before[2], "destination_bytes": destination.stat().st_size,
                       "elapsed_s": round(time.monotonic() - began, 2),
                       "limits": "All SQLite structures checked; payload hashes sampled. "
                                 "The runtime verifies every payload it actually queries."}
            print(json.dumps(summary, indent=2), flush=True)
            return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--max-runtime", type=float, default=600)
    args = parser.parse_args()
    if not math.isfinite(args.max_runtime) or args.max_runtime <= 0:
        parser.error("--max-runtime must be positive and finite")
    recover(args.source, args.destination, args.max_runtime)


if __name__ == "__main__":
    main()
