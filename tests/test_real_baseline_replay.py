"""Focused checks with the user's real recording and saved observations.

No generated audio/tracks, ADS-B downloads or geographic search. Set
DOPPLERGEIST_REAL_BASELINE, DOPPLERGEIST_REAL_INTAKE and DOPPLERGEIST_REAL_AUDIO
to run; unset inputs leave this optional external-data check skipped.
"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dopplergeist.automatic import analyze
from dopplergeist.discovery import discover_events
from dopplergeist.intake import IntakeRecording
from dopplergeist.matching import prepare_events

INPUT_NAMES = ("DOPPLERGEIST_REAL_BASELINE", "DOPPLERGEIST_REAL_INTAKE",
               "DOPPLERGEIST_REAL_AUDIO")


@unittest.skipUnless(all(os.environ.get(name) for name in INPUT_NAMES),
                     "Requires existing real recording, intake and baseline")
class RealBaselineReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = Path(os.environ[INPUT_NAMES[0]]).resolve()
        cls.intake = Path(os.environ[INPUT_NAMES[1]]).resolve()
        cls.audio = Path(os.environ[INPUT_NAMES[2]]).resolve()
        cls.saved = json.loads(cls.baseline.read_text())
        cls.recording = IntakeRecording(
            cls.intake / "recording.f32",
            json.loads((cls.intake / "recording.json").read_text()),
            json.loads((cls.intake / "intake.json").read_text()),
            cls.audio,
        )
        with cls.audio.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != cls.saved["source_sha256"]:
            raise ValueError("Real baseline does not belong to the supplied audio")
        cls.expected_groups = len(prepare_events(cls.saved["events"])[0])

    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="doppler-real-replay-")
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "replay"
        self.environment = patch.dict(
            os.environ, {"DOPPLERGEIST_BASELINE_JSON": str(self.baseline)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_replay_keeps_observations_groups_and_both_main_flyovers(self):
        before = self.baseline.read_bytes()
        phases = []
        events = discover_events(
            self.recording, output_dir=self.output, plot_summary=False,
            progress_callback=lambda **details: phases.append(details["phase"]))
        self.assertEqual(events, self.saved["events"])
        self.assertEqual(len(prepare_events(events)[0]), self.expected_groups)
        for moment in (2156, 3072):
            self.assertTrue(any(e["start_s"] <= moment <= e["end_s"] for e in events))
        self.assertEqual(self.baseline.read_bytes(), before)
        report = json.loads((self.output / "discovery.json").read_text())
        self.assertEqual(report["replay_provenance"]["json_sha256"],
                         hashlib.sha256(before).hexdigest())
        self.assertEqual(phases, ["grouping_proposals"])

    def test_rejects_audio_identity_mismatch_before_writing(self):
        metadata = dict(self.recording.metadata, source_sha256="0" * 64)
        recording = replace(self.recording, metadata=metadata)
        with self.assertRaisesRegex(ValueError, "different audio"):
            discover_events(recording, output_dir=self.output, plot_summary=False)
        self.assertFalse(self.output.exists())

    def test_refuses_existing_output_without_overwriting_it(self):
        self.output.mkdir()
        target = self.output / "discovery.json"
        target.write_bytes(self.baseline.read_bytes())
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            discover_events(self.recording, output_dir=self.output, plot_summary=False)
        self.assertEqual(target.read_bytes(), before)

    def test_requires_explicit_output_directory(self):
        with self.assertRaisesRegex(ValueError, "requires an output directory"):
            discover_events(self.recording, plot_summary=False)

    def test_rejects_baseline_with_no_groups(self):
        empty = dict(self.saved, events=[])
        path = Path(self.temporary.name) / "empty-groups.json"
        path.write_text(json.dumps(empty))
        with patch.dict(os.environ, {"DOPPLERGEIST_BASELINE_JSON": str(path)}):
            with self.assertRaisesRegex(ValueError, "no matching groups"):
                discover_events(self.recording, output_dir=self.output, plot_summary=False)
        self.assertFalse(self.output.exists())

    def test_actual_intake_reports_keyword_message_and_stops_before_search(self):
        reports = []

        def progress(stage, fraction, **details):
            reports.append((stage, fraction, details))

        # No first-sample UTC is supplied: the real orchestrator must request it,
        # after real FFmpeg decoding and replay, before acquiring any data.
        request = {"schema_version": 1, "audio_path": str(self.audio)}
        with patch("dopplergeist.automatic._search",
                   side_effect=AssertionError("Geographic search must not run")):
            result = analyze(request, self.output, progress=progress)
            # Resume uses the saved, verified discovery even without the replay
            # environment variable, and must keep its experimental label.
            with patch.dict(os.environ):
                os.environ.pop("DOPPLERGEIST_BASELINE_JSON", None)
                resumed = analyze(request, self.output, progress=progress, resume=True)
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(resumed["status"], "needs_input")
        self.assertEqual(result["reason_code"], "recording_time_required")
        self.assertEqual(result["events"], self.saved["events"])
        self.assertTrue(any(stage == "intake" and
                            details.get("message", "").startswith("Decoding audio:")
                            for stage, _, details in reports))
        provenance = json.loads((self.output / "provenance.json").read_text())
        self.assertEqual(provenance["event_selection"], "experimental_cached_baseline")
        self.assertEqual(provenance["resumed_from"], "audio_complete")
        self.assertEqual(provenance["discovery_replay"]["mode"],
                         "experimental_cached_baseline")
        self.assertFalse((self.output / "search").exists())


if __name__ == "__main__":
    unittest.main()
