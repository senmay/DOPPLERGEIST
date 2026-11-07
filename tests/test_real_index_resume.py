"""Crash/restart software checks using committed, observed C172/H500 rows.

Run with stdlib unittest; no generated aircraft observations or downloads.
These checks establish index equivalence, not geolocation accuracy.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from dopplergeist import data_access as access


SOURCE = (Path(__file__).resolve().parents[1]
          / "research/trajectory-motion/frozen/canonical-observations.json")


def inputs():
    frame = pd.DataFrame(json.loads(SOURCE.read_text()))
    frame = frame.loc[frame.typecode.isin(["C172", "H500"])].copy()
    sources = [{"path": str(SOURCE), "sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest()}]
    coverage = {"bounds": [-180., -90., 180., 90.],
                "time_range_s": [float(frame.time_s.min()), float(frame.time_s.max())],
                "all_aircraft": False, "source_note": "Archived real observations; incomplete excerpt"}
    return frame, sources, coverage


def batches():
    frame, _, _ = inputs()
    for offset in range(0, len(frame), 17):
        clean, invalid, altitude = access._prepare_staging(frame.iloc[offset:offset + 17], {})
        yield list(access._staging_rows(clean)), invalid, altitude


def no_frames():
    raise AssertionError("A completed ingest must not reopen/decode source archives")
    yield


def build(path, frames=None, callback=None, workers=1, **overrides):
    _, sources, coverage = inputs()
    options = dict(sources=sources, coverage=coverage, prepared=True,
                   progress_callback=callback)
    options.update(overrides)
    with patch.object(access, "_INDEX_CHECKPOINT_SECONDS", 0), \
            patch.object(access, "_index_workers", return_value=workers):
        return access.FlightIndex.build(path, batches() if frames is None else frames, **options)


def contents(path):
    with sqlite3.connect(path) as db:
        return tuple(db.execute(sql).fetchall() for sql in (
            "SELECT name,start,end,payload,payload_hash FROM segments ORDER BY name",
            "SELECT name,west,east,south,north FROM segments JOIN spatial USING(id) ORDER BY name",
            "SELECT * FROM prepared_tracks ORDER BY id",
            "SELECT value FROM metadata"))


def die():
    os.kill(os.getpid(), signal.SIGKILL)


def child(path, point):
    def progress(**event):
        if not event.get("checkpoint"):
            return
        phase = event["phase"]
        if ((point == "ingest" and phase == "staging_samples" and event["batches"] == 1)
                or (point == "segments" and phase == "building_segments" and event["flights"] == 1)
                or point == phase):
            die()

    original = sqlite3.connect

    class CrashConnection(sqlite3.Connection):
        insert_batches = 0

        def executemany(self, sql, parameters):
            result = super().executemany(sql, parameters)
            if sql.startswith("INSERT INTO samples"):
                self.insert_batches += 1
                if point == "inside_ingest" and self.insert_batches == 2:
                    die()
            return result

        def execute(self, sql, parameters=()):
            if point == "inside_vacuum" and sql == "VACUUM":
                steps = 0

                def interrupt():
                    nonlocal steps
                    steps += 1
                    if steps == 50:
                        die()
                    return 0

                self.set_progress_handler(interrupt, 1)
            result = super().execute(sql, parameters)
            if ((point == "inside_drop" and sql == "DROP TABLE samples")
                    or (point == "inside_prepare" and sql.startswith("INSERT INTO prepared_tracks"))):
                die()  # Kill after a write, before its transaction/checkpoint commit.
            return result

    def connect(*args, **kwargs):
        kwargs["factory"] = CrashConnection
        return original(*args, **kwargs)

    with patch.object(access.sqlite3, "connect", side_effect=connect):
        build(path, callback=progress)
    raise AssertionError(f"Did not reach interruption point {point}")


class RealIndexResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="doppler-real-resume-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.expected = contents(build(self.root / "complete.sqlite").path)
        self.assertGreater(len(self.expected[0]), 1)

    def crash(self, path, point):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                 "--child", str(path), point], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
        self.assertFalse(path.exists())
        self.assertTrue(path.with_suffix(".building").is_file())

    def assert_complete(self, result):
        self.assertEqual(contents(result.path), self.expected)
        with result.connect() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(db.execute("SELECT rtreecheck('spatial')").fetchone(), ("ok",))
        self.assertFalse(result.path.with_suffix(".building").exists())
        self.assertEqual(len(list(result.query(result.report["coverage"]["bounds"],
                                             result.report["coverage"]["time_range_s"]))),
                         len(self.expected[0]))

    def test_sigkill_resume_at_each_durable_stage(self):
        for point in ("ingest", "creating_sample_lookup", "segments", "dropping_samples",
                      "preparing_tracks", "compacting_flight_index", "publishing_flight_index"):
            with self.subTest(point=point):
                path = self.root / f"{point}.sqlite"
                self.crash(path, point)
                frames = batches() if point == "ingest" else no_frames()
                self.assert_complete(build(path, frames=frames))

    def test_sigkill_during_uncommitted_writes_and_vacuum(self):
        for point in ("inside_ingest", "inside_drop", "inside_prepare", "inside_vacuum"):
            with self.subTest(point=point):
                path = self.root / f"{point}.sqlite"
                self.crash(path, point)
                frames = batches() if point == "inside_ingest" else no_frames()
                self.assert_complete(build(path, frames=frames))

    def test_resume_skips_already_committed_flights(self):
        path = self.root / "skip.sqlite"
        self.crash(path, "segments")
        with sqlite3.connect(path.with_suffix(".building")) as db:
            state = json.loads(db.execute("SELECT value FROM build_state").fetchone()[0])
        called = []
        original = access._build_flight_segments

        def tracked(flight, *args):
            called.append(flight)
            return original(flight, *args)

        with patch.object(access, "_build_flight_segments", side_effect=tracked):
            self.assert_complete(build(path, frames=no_frames()))
        self.assertTrue(called)
        self.assertTrue(all(flight > state["last_flight"] for flight in called))

    def test_parallel_resume_matches_serial(self):
        path = self.root / "parallel.sqlite"
        self.crash(path, "segments")
        self.assert_complete(build(path, frames=no_frames(), workers=2))
        self.assert_complete(build(self.root / "parallel-fresh.sqlite", workers=2))

    def test_changed_sources_scope_and_mode_preserve_checkpoint(self):
        path = self.root / "mismatch.sqlite"
        self.crash(path, "segments")
        partial = path.with_suffix(".building")
        before = partial.read_bytes()
        for overrides in ({"sources": []}, {"prepare_scope": ((-74, 40, -73, 41), (0, 1))},
                          {"prepared": False}, {"max_gap_s": 31.0}):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(access.DataUnavailable, "different inputs"):
                    build(path, frames=no_frames(), **overrides)
                self.assertEqual(partial.read_bytes(), before)

    def test_legacy_and_corrupt_partials_are_never_deleted(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                path = self.root / f"legacy-{corrupt}.sqlite"
                partial = path.with_suffix(".building")
                if corrupt:
                    partial.write_bytes(b"interrupted legacy database")
                else:
                    with sqlite3.connect(partial) as db:
                        db.execute("CREATE TABLE metadata(value TEXT)")
                before = partial.read_bytes()
                with self.assertRaisesRegex(access.DataUnavailable, "Unrecognized"):
                    build(path, frames=no_frames())
                self.assertEqual(partial.read_bytes(), before)

    def test_python_exception_keeps_checkpoint_and_published_index_reuses_cache(self):
        path = self.root / "exception.sqlite"

        def stop(**event):
            if event["phase"] == "dropping_samples":
                raise RuntimeError("operator stop")

        with self.assertRaisesRegex(RuntimeError, "operator stop"):
            build(path, callback=stop)
        self.assertFalse(path.exists())
        self.assert_complete(build(path, frames=no_frames()))
        self.assert_complete(build(path, frames=no_frames()))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        child(Path(sys.argv[2]), sys.argv[3])
    else:
        unittest.main()
