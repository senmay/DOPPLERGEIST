# Experimental replay of saved acoustic proposals

`DOPPLERGEIST_BASELINE_JSON` optionally reuses a saved discovery JSON for the
same unchanged audio. This is a research control for separating discovery from
later matching stages. With the variable unset, normal discovery is unchanged.

The replay validates the audio SHA-256, every event, and matching-group
preparation. It requires an explicit output directory and refuses to overwrite
an existing discovery result. It preserves all saved observations and their
recording-relative timestamps, including harmonics and later flyovers.
`replay_provenance` records the source JSON path and content hash. Automatic
orchestration also labels `event_selection` as `experimental_cached_baseline`
and includes `discovery_replay` in its provenance, including on audio resume.

The intake progress adapter passes its human-readable message as a keyword.
The job progress callback accepts only the stage and fraction positionally;
passing the message as a third positional argument previously failed while
decoding real media.

## Running against existing data

Use a new job directory and an independently justified first-sample time window.
Keep the audio unchanged; replay rejects a different audio hash. Do not reuse
saved timestamps after cutting the audio. For example, with appropriately
prepared offline flight indexes and terrain:

```bash
export DOPPLERGEIST_BASELINE_JSON=/absolute/path/to/baseline/discovery.json
dopplergeist analyze /absolute/path/to/unchanged-recording.webm \
  --start-utc "$START_UTC" --end-utc "$END_UTC" \
  --time-source "$CLOCK_EVIDENCE" \
  --config /absolute/path/to/offline-operator.json \
  --jobs /absolute/path/to/new-jobs --max-runtime 900
```

This does not download or complete missing indexes by itself. Offline mode can
still rebuild indexes from cached archives; use explicit finished `flight_indexes`
when a short test must exclude archive ingestion. Regional receiver bounds do
not reduce the size of daily ADS-B archives. Preserve an unfinished `.building`
file before any operation that could replace it.

A replay does not validate the advanced detector, establish flight identity,
or turn a ranking into an accepted location. Bounds informed by a reference
point make the experiment a known-area test. The package cannot infer how a
user selected bounds; record that prior in the experiment separately.

## Focused verification with the real Colsto recording

No pip install is needed to run these checks from a checkout. Use an existing
compatible Python environment, the original real audio, its saved intake and
baseline. This command imports the checkout through `PYTHONPATH`, without
replacing the installed package:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
DOPPLERGEIST_REAL_BASELINE=/absolute/path/to/baseline/discovery.json \
DOPPLERGEIST_REAL_INTAKE=/absolute/path/to/saved-job/intake \
DOPPLERGEIST_REAL_AUDIO=/absolute/path/to/colsto.webm \
/absolute/path/to/existing/python -m unittest discover \
  -s tests -p test_real_baseline_replay.py -v
```

The external-data checks are skipped when these inputs are not configured;
missing configured files fail. They use saved real observations and FFmpeg
decoding, with no generated recordings/tracks, downloads or geographic search.
The integration case deliberately omits UTC and must return
`needs_input / recording_time_required` before acquisition. The remaining checks
cover both main flyovers, group preservation, mismatched audio, empty groups,
existing outputs, an explicit output requirement and replay provenance.

Local verification on 2026-11-07: **6 checks passed in 13.510 seconds**, using
the real 3600.1845-second WebM and baseline containing 18 ridges / 16 groups.
This verifies the intake/replay behavior, not native search backends,
localization accuracy or nationwide recall. The synthetic test suites were not run.

The checkout starts from fork revision `afdb2bd0c82127de93405407c8e3d51a9574f342`.
It is newer than the installed revision `94c687f20d69a41b08c7c39295dc5cb670cbdc77`
and includes changed flight-selection policy and detector code. Do not silently
replace that installed environment or edit old checkpoint identities to accept
this checkout. Use a separate environment/output for any later migration.
