# Historical inputs and bounded search

`data_access.py` and `tiled_search.py` connect the shared request contracts to the
physical scorer; the legacy manual research workflow remains reproducible.

## Acquisition

`acquire_inputs(request, recording, cache_root=..., offline=False)` validates the
shared request/recording dictionaries. A defensible recording-start UTC window
is required. Acquisition covers that window plus the complete recording and
sound propagation guard, including adjacent UTC days. `FlightCache.acquire_day`
obtains ADSB.lol release metadata, resumes split archive parts using checked HTTP
ranges, checks provider size/SHA-256, and streams gzip readsb traces into an index.
The source archive and existing indexes can contain all aircraft types. New daily indexes retain only confirmed non-jet traces. Before CPA search, only records classified as piston or turbine are selected; known jets and unknown or conflicting types are excluded. Classification uses cached ICAO type metadata when available. This is a narrower search scope, and incomplete or incorrect type metadata can hide relevant non-jet flights. The archive is downloaded once per day/provider;
later uploads reuse the derived index without opening/downloading the archive.
Both older single `.tar` assets and newer split `.tar.aa`/`.tar.ab` assets are read
through the same streaming archive reader. Existing indexes are filtered before search. Type metadata is discovered in the index's parent cache directories, or can be supplied with `filters.aircraft_type_database`. Tracks with unknown or conflicting type metadata are excluded; without usable classification the search may have no eligible aircraft.

New daily archive indexes prepare traces and compute flight-segment geometry in
separate processes, using the available CPU cores minus one for the SQLite writer
(and at least one worker). Queued work is bounded and results are written in
source order. SQLite writes and finalization still run in the writer process.
New builds use a SQLite rollback journal with full synchronization and keep a
transactional `build_state` in the `.building` file. Sample and flight progress
is committed every 128 batches/flights or after five seconds, at the next item
boundary. Long individual SQL operations are not five-second checkpoints.
All segments are committed before dropping samples; optional track preparation
and compaction have separate durable stages. Progress marks actual commits with
`checkpoint=true` and reports `dropping_samples` explicitly.

Repeat the same build with the same sources, runtime/builder, staging batch
sequence and preparation scope to resume. An interrupted ingest must decode and
skip its committed source batches; after ingest completes the frames iterator is
not reopened. Completed flights, including those producing no segments, are
skipped by their committed flight key. Optional preparation and unfinished SQL
operations can repeat. Rollback/recovery may take time and need disk space;
dropping a large sample table can require a rollback journal approaching that
table's size, and VACUUM additionally needs room for another database copy.
This is crash recovery, not protection from faulty storage or corrupt inputs.

Unrecognized legacy partials and incompatible checkpoints are rejected without
deletion. Preserve a legacy `.building` and choose a different output path; this
change cannot resume a file corrupted by the old `journal_mode=OFF` builder.
Only a completed index is atomically published, without replacing an existing
destination. Archive downloads remain global per UTC day regardless of receiver
bounds. `FlightCache(..., offline=True)` reuses cached metadata/archive parts and
refuses missing downloads. Changing code does not update a separately installed
package: use the chosen checkout explicitly and keep its normalization and
aircraft-policy metadata consistent with the search configuration.

The focused crash checks use 134 archived C172/H500 observations (six segments),
kill the writer at stage boundaries and inside uncommitted writes/VACUUM, then
compare raw/prepared payloads, spatial bounds and metadata against an uninterrupted
build. They also exercise parallel resume, SQLite integrity and mismatch refusal:

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 \
  python -m unittest discover -s tests -p test_real_index_resume.py -v
```

These are small software checks, not full-day throughput or location validation.

For a fresh automatic regional upload, only segments touching its receiver bounds
and emission-time interval get the optional numeric decoding cache. Every eligible confirmed non-jet
segment remains in the index, and queries outside that warm scope use the
verified original payload. This reduces first-use cache construction without
changing source coverage or filtering aircraft. `FlightIndex.prepare` also accepts
`prepare_scope=(bounds_wsen, time_range_s)` for an explicitly scoped index copy.
Prepared indexes from the source versions immediately before and after the
cache-size change remain usable when the exact source and all other builder
identities match. Unknown source versions continue to use the verified original
payload instead of trusting incompatible prepared data.

An optional native collector speeds canonical raw-index column extraction;
unsupported representations and installations without the extension use the
existing Python path. The exact compatible prepared-index versions remain usable.
On one real full-day query, 1,060 trajectories decoded with identical fields in
median 6.31 seconds of CPU before and 5.79 seconds after. This measures the query
only, not a complete upload or national search.

Decoded tracks also have a bounded 128 MiB serialized cache per index per worker,
with at most 2,048 entries. `FlightIndex(track_cache_bytes=...)` accepts a smaller
limit or zero; cached queries still verify source payloads and return independent
objects. On a real Kane County tile with 580 tracks and 1,337 associations, six
alternating warm replays reduced median CPU from 5.486 to 5.212 seconds versus
32 MiB, preserving the complete payload and retention checksum. This trades up to
96 MiB more storage per index per worker for less decoding; the measured 5.0%
reduction is specific to this tile, not an end-to-end or national estimate (#303).

The on-disk SQLite index has spatial and time indexes, source hashes, explicit
source scope, per-payload checksums and altitude/classification labels. Disk staging supports
arbitrary input order. Query predicates select whole gap-split segments; they
never clip geometry or extrapolate across a gap. Daily release namespaces prevent
ID collisions. Segments on separate days are not joined across midnight; a ridge
requiring unsupported geometry is rejected by the physical model.

Version-two bounds enclose complete Hermite curves using ECEF cubic Bezier
control hulls, including excursions beyond measured latitudes/longitudes. The
normalizer hash versions raw caches and derived bounds. Legacy sample-bounds
indexes disable spatial pruning; oversized queries fail visibly under the search
budgets. Rebuild those indexes for efficient queries. Missing velocity components
use the shared segment-local inference helper, preserving finite measurements
including zeros. Legacy Parquets that already replaced missing velocities with
zero require raw re-ingestion before claiming corrected motion.

`scripts/index_flights.py` restores an index from a supplied Parquet. Its required
bounds, UTC interval and source note describe ingestion coverage. Use
`--all-aircraft` only for sources that retained jets and unknown types. The
historical prop/unknown table must be declared incomplete. Corrupt/truncated
inputs raise `DataUnavailable`; this is distinct from no acoustic events.

`CachedTerrain` acquires the [GLO-90 catalogue and tiles](https://copernicus-dem-90m.s3.amazonaws.com/readme.html)
and the [EGM2008 geoid](https://cdn.proj.org/). Each file has a checksum receipt.
GLO-90 heights are converted to ellipsoidal heights by adding geoid undulation.
Offline mode never fetches missing files. Only catalogue-absent tiles are treated
as ocean; missing downloads and nodata are explicit failures. Existing terrain
caches require the operator's explicit `adopt_existing=True` and format validation
before receipts are created. A caller-supplied geoid must use EGM2008; the old
regional EGM96 approximation is not the default automatic input.

Sampling reads local raster windows, including for the global geoid, and respects
the published half-pixel COG tile shift at exact degree boundaries.

The returned `AcquiredInputs` has `.indexes`, `.terrain`, `.bounds`,
`.time_range_s` (absolute emission window), `.recording_start_window_s`, and
`.coverage`. ADS-B query coverage does not establish receiver completeness or
prove that every audible flight was observed. Copernicus is a surface model,
which also leaves microphone height uncertain.

## Search

Use `matching.prepare_events(detected_events)` to get legacy event settings and
measurements, then call:

```python
report = search_national(
    inputs.indexes, inputs.terrain, events, measurements,
    time_range_s=inputs.time_range_s, output_dir=output / "search",
    bounds=inputs.bounds, budget=SearchBudget(),
    scoring={"mode": "robust_ridge",
             "recording_start_window_s": inputs.recording_start_window_s},
)
```

There is one EPSG:5070 origin and integer grid. Half-open tile cores own receiver
cells exactly once; expanded queries retain boundary flight segments. Original
flight geometry, emission-time propagation,
event offsets and score units are identical in every tile. One shared clock is
constrained to the supplied recording-start window. Coarse tiles use separate
worker processes for the built-in cached-terrain provider, automatically choosing
one fewer worker than the available logical CPUs, with a minimum of one. The
upload operator can override that count with the positive integer `search_workers`.
Custom terrain providers use serial execution. Hard limits on grid cells, tracks, samples,
CPA rows and candidate combinations bound each worker's allocations; total memory
grows with the worker count. An overloaded track query is retried with the same
conservative observer-distance test used by the scorer. Tracks with unsupported
motion remain in the result so their uncertainty is recorded. If a recognized
capacity limit is still exceeded, the search divides the tile into smaller
nonoverlapping grid rectangles and checkpoints each completed piece. Fine search
also divides overloaded rectangles.
If an unknown-clock, robust subset search still exceeds its association-count
or association work/storage limit at one cell, it partitions first-passage clock
anchors into checkpointed,
half-open ranges. Each candidate belongs to one range and retains its full fit
interval. Other single-cell capacity failures, missing data, and errors remain
visible as incomplete work. Coarse capacity failures also write a separate
`capacity-TILE.json` with the exact reason; existing subdivision marker formats
and resume validation stay unchanged. The work/storage path is checked by
`tests/test_real_association_work_partition.py` against an actual recorded job,
including replay of its clock-child checkpoint. It requires
`DOPPLERGEIST_REAL_WORK_JOB` and `DOPPLERGEIST_REAL_WORK_CONFIG`; it is a bounded
software check, not location-accuracy evidence.
Reducing `tile_cells` trades scheduling overhead for
smaller working sets.

On one overloaded full-day cell from the original recording and historical
traffic, eight clock ranges reproduced all 23,136 unpartitioned associations;
142 tracks had unsupported motion. A separate real-data engineering replay
matched leading scores, resumed saved ranges, rejected a corrupt split marker,
and imported two ranges computed on another host. These checks establish neither
a nationwide runtime nor a location.

Workers retain private score ledgers. The parent verifies their identities and
checksums before importing a completed tile, and merges rankings in deterministic
tile order. Parallel tiles use a conservative per-tile score floor; a checkpoint
with that policy can resume with a different worker count, including one. A
serial run using a global floor has a different checkpoint identity. On one
shared-machine replay of two real grid points from a full-day search, one worker
took 64.93 seconds and two took 38.95 seconds including startup, with identical
payloads and ledgers. This small sample does not predict nationwide runtime.

Retention-ledger connections wait up to 30 seconds for a transient SQLite
lock, matching the worker-stage import. Longer contention can still fail a run;
this does not replace the stopped-job snapshot procedure or save work that has
not been checkpointed.

Before the spline-hull improvement below, one real full-day grid cell's track
retry reduced 1,929 queried tracks to 614 within the configured memory limit,
retaining all 200 tracks with unsupported motion. Complete scoring with the full
and reduced sets produced identical results, support records and retention
checksums. Other overloaded cells may still exceed the limits.

With the spline-hull proof enabled, the same cell retained 426 tracks, including
12 unsupported tracks. Full scoring with the original and filtered sets again
produced identical results and retention checksums; the retry took 40.66 CPU
seconds, compared with about 39.9 CPU seconds before the change.

For a trajectory with unsupported motion, the distance sieve can still exclude
it when the bounding boxes of every cubic-spline control hull are outside the
observer set's search radius. This proof does not assume subsonic motion. In a
different full-day cell, it excluded 122 distant unsupported trajectories
while leaving 20 unsupported trajectories visible; all 23,136 supported
associations were unchanged. One track-query timing was about six CPU seconds
with either version, so this is a coverage-accounting improvement, not a
demonstrated nationwide speedup.

Candidate scores are merged deterministically with approach identities, then
global finalists are refined on the same global grid in bounded subtiles. The
coarse spacing is a computational budget, not a proof that an isolated minimum
could not be missed between cells. Keep this limitation visible in confidence
decisions. `top_k` limits retained candidates; raw scores are not probabilities.

Robust subset scoring visits larger associations first **within each receiver
point**, preserving fit-cache locality. Every accepted group contributes at most
one nonnegative score unit. The current kth score from distinct, fully assessed
modes therefore supplies a safe lower cutoff: an association with too few groups
cannot enter Top-K. While fitting a shared-clock trial, validated residuals can
tighten that ceiling or establish a residual rejection before the remaining
groups are fitted. Comparisons are strict, so ties remain eligible. Acoustic
batches for later groups are deferred until a surviving trial needs them.

At each event group, the scorer fits only the shared-clock trials that survive
earlier groups and the acoustic bound. It reuses validated group contributions
and preserves the order and upward rounding of the conservative score ceiling.
If the product of clock trials and event groups exceeds 4,096, the scorer keeps
the scalar traversal.

Batch arithmetic matches the scalar model and uses Python `float` powers.
Regression tests compare every fitted clock in the recorded-data test batches
with the scalar fitter. Unsupported, nonfinite or degenerate batches use scalar
fitting. The same fallback handles overflowing powers, zero drift bounds and
noncanonical numeric parameters.

A replay at one source-informed receiver point used 16 event groups and 3,905
associations. Across three alternating pairs, median CPU time fell from 68.9247
to 25.5895 seconds, a 2.6935× speedup. All candidate results, diagnostics, support
records and retention data matched the baseline with the separate correction to
scalar rounding. A narrower clock window had a smaller change: 4.6782 to 4.0092 CPU
seconds. The [performance report](PERFORMANCE.md) gives the inputs, procedure and
limits.

When an event's conservative loss bound alone makes the association unable to
reach the current Top-K floor, the scorer may also skip that clock and retain a
finite score ceiling. This applies only when the other matched groups cannot
make up the deficit even with perfect scores. Strict comparisons preserve ties;
the result verifier distinguishes these finite ceilings from outright acoustic
rejections. On one full-day point with 468 real associations, retained candidates
matched exactly and CPU time changed from 37.77 to 28.23 seconds. The persisted
bound ledger differs because it accounts for additional skipped trials. This
single measurement does not establish nationwide runtime or accuracy. A separate
2,443-association full-day clock partition retained the same candidates, with
CPU time changing from 158.01 to 149.56 seconds in one paired run. Both runs
reported zero unbounded candidate assessments; their ledgers differed as expected
for the additional score ceilings.

For association sets of at least 1,000 candidates, repeated acoustic batches
reuse exact track, receiver, time-array and sound-speed inputs in a per-worker
32 MiB cache. Sparse searches keep the ordinary path. A complete real-data clock
partition with 3,795 associations took 76.05 → 74.60 CPU seconds with identical
payloads and retention checksums. A complete 19,341-association partition took
276.99 → 250.88 CPU seconds, also with identical payloads and retention
checksums. A 5,000-association sample took 90.45 → 74.21 CPU seconds with
identical scored rows. These measurements are specific to those workloads, not
nationwide runtime estimates.

Unperformed trials remain unknown with explicit conservative bounds in the
retention ledger. A competitive candidate receives full evaluation, preserving
its original fitted diagnostics. The initial zero cutoff only rejects trials
that cannot satisfy the existing residual limits. This does not narrow the
automatic speed/range constraints or establish completeness between grid cells.

Real-observation computation replays for PR #152 measured the following; neither
is an independent localization-accuracy test:

| Replay | Source fits, before → after | Acoustic calls, before → after | Retained results |
| --- | --- | --- | --- |
| 169-point refinement neighborhood, 1,859 associations | 14,196 → 14,196 | 3,380 → 1,963 | All 100 identical, including diagnostics and best discarded score |
| 256 evenly spaced associations from 10,549 at one saved receiver point | 30,361 → 19,629 | 879 → 689 | All 92 identical, with conservative omitted-trial bounds |

The first replay took 20.13 → 15.86 seconds in one paired run. The second saved
about 8–28% wall time across paired runs under varying machine load; operation
counts were stable. These measurements do not predict nationwide runtime. The
group-count ceiling alone did not show a reliable speedup on the existing cache.

With the acoustic screen enabled in both runs, one full-day replay of a single
real grid point and 468 associations took 56.28 CPU seconds using scalar
first-event fits and 7.72 seconds when fitting only eligible clocks in batches.
The complete tile payload and retention checksum matched. This is one point,
not a nationwide runtime estimate or an independent location validation.

Manifest and tile receipts hash configuration, input index files, event
measurements, implementation and terrain files. Interrupted jobs reuse completed
tiles with the same identity. Changed inputs require a new output directory.
Failed tiles are retried on resume; `max_tiles` is a fixed configuration budget.
Within one search process, terrain receipt validation reuses up to 256 verified
file digests while device, inode, size, modification time and change time remain
unchanged. A changed signature causes a full rehash; a file that changes during
hashing fails the tile check. This avoids re-reading the same geoid for every
saved tile. The reuse relies on filesystem metadata identifying changes; it
cannot detect content changes that leave the entire signature unchanged. It does
not make checkpoints from a different implementation automatically compatible.
On the real 80,585,622-byte geoid, four hash checks took 0.468 CPU seconds with
four full reads versus 0.113 seconds with one read; this single-raster test is
not a complete resume or nationwide timing. See the
[measurement](../benchmarks/results/terrain-resume-hash-20261103.json) and
`scripts/benchmark_terrain_resume_hash.py`.
Reports distinguish complete/partial computation, source scope, missing tiles,
refinement failure, runtime and Linux process peak RSS. A complete tile is never
labelled a complete national search. The product/confidence workers translate
this internal report to the shared coverage and result contracts.
On Linux, process peak RSS uses the process's post-exec `VmHWM`; it does not
include worker processes or FFmpeg. Earlier `getrusage` receipts can inherit a
larger pre-exec high-water mark and should not be compared as measured peaks.
Search progress counts grid-cell work, not elapsed time or a guarantee that all
data and model support are complete; dense cells may take much longer to process.

Source completeness includes an acoustic halo outside observer bounds: a
CONUS-cropped flight table cannot establish complete border coverage.
`filters={"association": "subsets"}` delegates bounded event/group subset
association to the matching worker and preserves unmatched proposals.
`alternative_modes_exhaustively_assessed` remains false because coarse-grid and
top-k retention do not establish exhaustive alternative-mode recall.

Automatic CPA proposals retain speeds from zero through the sound speed and
distances from zero through the configured maximum; unsupported motion is still
reported rather than silently treated as valid. In one real full-day cell, five
passes below the former 1 m/s minimum added 1,166 associations, retaining all
23,136 previous associations. These are proposals, not confirmed aircraft sounds.
The separately defined finite emitted-frequency range remains a modeling prior.

The normal `robust_ridge` path defaults to subset association over **all prepared
independent groups**. There is no default maximum of two or three matched events.
Harmonics/fragments are grouped by the audio/matching producers before this
driver. False proposals can remain unmatched while later supported groups enter
the same joint score and refined grid. Legacy `ridge`/`brightness` paths retain
full-event association; `filters={"association": "full"}` also selects it
explicitly. A deliberately smaller `max_events` is a work limitation: the report
sets `all_event_groups_considered=False`, explains the cap and returns partial
status. Candidate/branch budget exhaustion likewise cannot count as completion.

For a supplied time window, shared CPA-consistency intervals are intersected with
the trusted window before scoring. A biased event-center estimate cannot discard
a common clock inside both intervals merely because it lies beyond a five-second
anchor sweep. A trusted exact clock stays exact; events never receive separate
clock adjustments. The current acceptance target is up to 1,000 m error, with
100-500 m or closer preferred. The driver reports physical/data/work limits rather
than converting scores into that accuracy claim.

## Checks

Use actual recorded observations; do not run the historical generated-data suites.

```bash
DOPPLERGEIST_REAL_CPA_FIXTURE=/path/to/real-cpa-diagnostic.json \
  PYTHONPATH=src python -m pytest -q \
  tests/test_real_retention_floor.py tests/test_real_score_ceiling_validation.py

PYTHONPATH=src OPENBLAS_NUM_THREADS=1 python scripts/benchmark_acoustic_sieve.py \
  --fixture /path/to/real-cpa-diagnostic.json --baseline 72cdebb \
  --output results/my-new-sieve-comparison
```

The diagnostic contains measured event ridges, observed `cpa_rows`, one receiver
`point` and the local `real_index` path. The benchmark can override that path with
`--index`; no media or native index is bundled. It compares isolated baseline
matching/scoring modules with the current fitter and requires identical shared
geometry. Timing includes scoring with a bounded fit cache. Scaling experiments
and location-accuracy evidence must be labelled separately.

