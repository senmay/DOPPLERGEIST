"""Sequential, checkpointed national search over one global projected grid.

Every tile sees all overlapping original flight segments and the same event
clocks, propagation model and scoring units. Overloaded single cells may also
partition association anchors without narrowing the physical fit intervals.
"""
from collections import OrderedDict
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib.metadata import version
import hashlib
import itertools
import json
import math
import platform
from pathlib import Path
import time

import numpy as np
import pandas as pd
from pyproj import Transformer

from .data_access import CONUS, DataUnavailable, cached_sha256, content_hash, sha256, write_json
from .finite_search import RetentionLedger, bounded_assessment, classified_assessment, identify_mode, mode_seed
from .resource_usage import process_peak_rss_kib
from .geometry import ACOUSTIC_BACKEND, AcousticSupportError, ecef
from .matching import RIDGE_FIT_BACKEND, _cache_ridge_fits
from .native_cpa import CPA_BACKEND
from .search import (_CPA_DTYPE, _PreparedCPATrack, _ordered_cpa_rows,
                     filter_association_event, pair_candidates, score_candidate)


def _verified_terrain_hash(path, cache):
    """Reuse a digest only while the same file identity and timestamps hold."""
    path = Path(path)

    def identity():
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    before = identity()
    entry = cache.get(path)
    if entry is not None and entry[0] == before:
        cache.move_to_end(path)
        return entry[1]
    digest = sha256(path)
    if identity() != before:
        raise DataUnavailable(f"Terrain changed while hashing: {path}")
    cache[path] = (before, digest)
    cache.move_to_end(path)
    if len(cache) > 256:
        cache.popitem(last=False)
    return digest


def _support_error(error):
    result = {"track_id": error.track_id, "reason": error.reason}
    for name in ("emission_intervals_s", "receive_intervals_s"):
        intervals = getattr(error, name, None)
        if intervals is not None:
            result[name] = intervals
    return result


@dataclass(frozen=True)
class SearchBudget:
    spacing_m: float = 2000.0
    tile_cells: int = 32
    max_tracks_per_tile: int = 2000
    max_track_samples_per_tile: int = 500000
    max_cpa_rows: int = 100000
    max_candidates_per_tile: int = 20000
    top_k: int = 100
    refine_spacing_m: float = 100.0
    refine_finalists: int = 10
    cpa_step_s: float = 2.0
    max_tiles: int | None = None
    max_tile_evaluations: int | None = None

    def __post_init__(self):
        integer_fields = ("tile_cells", "max_tracks_per_tile", "max_track_samples_per_tile", "max_cpa_rows",
                          "max_candidates_per_tile", "top_k", "refine_finalists", "max_tiles",
                          "max_tile_evaluations")
        for name, value in asdict(self).items():
            if value is not None and (not np.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be positive and finite")
            if name in integer_fields and value is not None and type(value) is not int:
                raise ValueError(f"{name} must be an integer")
        if self.tile_cells > 256:
            raise ValueError("tile_cells must be <=256 (65536 grid points)")


def _key(row):
    # Distinct approaches remain distinct even when they share flight IDs.
    seed = row.get("mode_seed") or mode_seed(row)
    return (tuple(seed["event_indices"]), tuple(seed["flight_ids"]), tuple(seed["pass_emission_times_s"]),
            tuple(seed["position"]), seed["recording_start_time_s"])


class _RetainedScoreFloor:
    """A lower bound on the kth score from distinct, assessed modes only."""

    def __init__(self, top_k, initial=None):
        self.top_k, self.initial = top_k, initial
        self.scores = {}
        self.value = initial

    def observe(self, row):
        if (row.get("core_assessment_complete") is not True
                or row.get("score_kind") != "matched_group_support_minus_normalized_residual"
                or not math.isfinite(row["score"])):
            return
        identity, score = identify_mode(row), row["score"]
        if (identity not in self.scores and len(self.scores) == self.top_k
                and score <= min(self.scores.values())):
            return
        self.scores[identity] = max(score, self.scores.get(identity, -math.inf))
        if len(self.scores) > self.top_k:
            del self.scores[min(self.scores, key=self.scores.get)]
        if len(self.scores) == self.top_k:
            local = min(self.scores.values())
            self.value = local if self.initial is None else max(local, self.initial)


def _retained_score_floor(rows, top_k):
    # merge_candidates already returned distinct, ordered modes, at most top_k.
    if len(rows) != top_k or any(row.get("core_assessment_complete") is not True
            or row.get("score_kind") != "matched_group_support_minus_normalized_residual"
            or not math.isfinite(row["score"]) for row in rows):
        return None
    return min(row["score"] for row in rows)


def merge_candidates(rows, top_k, *, ledger=None, stage=None, bounds=()):
    unique, occurrences = {}, 0
    for row in rows:
        if not math.isfinite(row["score"]):
            continue
        occurrences += 1
        key = identify_mode(row)
        if key not in unique or row["score"] > unique[key]["score"]:
            unique[key] = row
    kept = sorted(unique.values(), key=lambda row: (-row["score"], _key(row)))[:top_k]
    if ledger is not None:
        if stage is None:
            raise ValueError("Retention accounting requires a stage identity")
        ledger.record(stage, list(unique.values()), kept, occurrences, bounds=bounds)
    return kept


def _grid_extent(bounds, spacing_m, crs):
    forward = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    left, bottom, right, top = forward.transform_bounds(*bounds, densify_pts=101)
    xmin, ymin = math.floor(left / spacing_m), math.floor(bottom / spacing_m)
    xmax, ymax = math.ceil(right / spacing_m), math.ceil(top / spacing_m)
    return xmin, ymin, xmax, ymax


def grid_tiles(bounds, spacing_m, tile_cells, crs="EPSG:5070"):
    """Half-open integer-cell cores: each global receiver cell has one owner."""
    xmin, ymin, xmax, ymax = _grid_extent(bounds, spacing_m, crs)
    for iy in range(ymin // tile_cells, ymax // tile_cells + 1):
        for ix in range(xmin // tile_cells, xmax // tile_cells + 1):
            yield f"{ix}_{iy}", (max(xmin, ix * tile_cells), max(ymin, iy * tile_cells),
                                  min(xmax + 1, (ix + 1) * tile_cells), min(ymax + 1, (iy + 1) * tile_cells))


def _split_tile_cells(cells):
    """Partition a half-open grid rectangle without dropping or repeating cells."""
    x0, y0, x1, y1 = cells
    xs = [x0, (x0+x1)//2, x1] if x1-x0 > 1 else [x0, x1]
    ys = [y0, (y0+y1)//2, y1] if y1-y0 > 1 else [y0, y1]
    if len(xs) == len(ys) == 2:
        return []
    return [(xs[i], ys[j], xs[i+1], ys[j+1])
            for j in range(len(ys)-1) for i in range(len(xs)-1)]


def _tile_capacity_error(error):
    """Only known per-tile work limits are candidates for subdivision."""
    return str(error) in {
        "Tile track/sample memory budget exceeded; reduce tile_cells",
        "Tile CPA-row memory budget exceeded",
        "Association work/storage budget exceeded; search is incomplete",
        "Association candidate budget exceeded; search is incomplete",
        "Tile candidate memory budget exceeded; reduce tile_cells",
    }


def _association_capacity_error(error):
    """Both enumeration and index construction can exhaust association work."""
    return str(error) in {
        "Association candidate budget exceeded; search is incomplete",
        "Association work/storage budget exceeded; search is incomplete",
    }


@lru_cache(maxsize=8)
def _cached_to_geographic(crs):
    return Transformer.from_crs(crs, "EPSG:4326", always_xy=True)


def _to_geographic(crs):
    try:
        hash(crs)
    except TypeError:
        return Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    return _cached_to_geographic(crs)


def _tile_coordinates(cells, spacing, bounds, crs, receiver_mask=None):
    x0, y0, x1, y1 = cells
    x, y = np.meshgrid(np.arange(x0, x1, dtype=np.int64), np.arange(y0, y1, dtype=np.int64))
    x, y = x.ravel() * spacing, y.ravel() * spacing
    lon, lat = _to_geographic(crs).transform(x, y)
    keep = (lon >= bounds[0]) & (lon <= bounds[2]) & (lat >= bounds[1]) & (lat <= bounds[3])
    if receiver_mask is not None:
        keep &= receiver_mask.contains(lon, lat)
    x, y, lat, lon = (a[keep] for a in (x, y, lat, lon))
    return pd.DataFrame({"point_id": np.arange(len(x)), "x": x, "y": y, "lat": lat, "lon": lon})


def _sample_tile_terrain(points, terrain):
    points["elev_m"] = (terrain.sample(points.lat.to_numpy(), points.lon.to_numpy())
                        if len(points) else np.array([]))
    return points


def _tile_dem_keys(cells, spacing, bounds, crs, catalog):
    """Conservatively predict DEMs touched by a tile for worker affinity."""
    x0, y0, x1, y1 = cells
    west, south, east, north = _to_geographic(crs).transform_bounds(
        x0 * spacing, y0 * spacing, x1 * spacing, y1 * spacing, densify_pts=21)
    west, south = max(west, bounds[0]), max(south, bounds[1])
    east, north = min(east, bounds[2]), min(north, bounds[3])
    if west > east or south > north:
        return ()
    south_key, north_key = math.floor(south - 1 / 2400), math.floor(north - 1 / 2400)
    west_key, east_key = math.floor(west + 1 / 2400), math.floor(east + 1 / 2400)
    keys = []
    for latitude in range(south_key, north_key + 1):
        for longitude in range(west_key, east_key + 1):
            tag = (f"Copernicus_DSM_COG_30_{'N' if latitude >= 0 else 'S'}{abs(latitude):02d}_00_"
                   f"{'E' if longitude >= 0 else 'W'}{abs(longitude):03d}_00_DEM")
            if tag in catalog:
                keys.append(tag)
    return tuple(keys)


def tile_points(cells, spacing, bounds, terrain, crs, receiver_mask=None):
    return _sample_tile_terrain(_tile_coordinates(cells, spacing, bounds, crs, receiver_mask), terrain)


def _expanded_bounds(cells, spacing, radius, crs):
    x0, y0, x1, y1 = cells
    # Padding includes sound range, spline excursions between gap-limited track
    # samples and supported projection distortion. Full payloads remain unmodified;
    # receiver-country masks must never clip this aircraft query envelope.
    margin = 1.6 * radius + 15000
    box = (x0 * spacing - margin, y0 * spacing - margin, x1 * spacing + margin, y1 * spacing + margin)
    return _to_geographic(crs).transform_bounds(*box, densify_pts=101)


def _track_geometry_digest(track):
    """Compare complete geometry without converting every float to JSON text.

    Track geometry uses binary64 arrays. Canonical byte order and explicit shapes
    preserve exact identities (including signed zero) across array layouts. This
    digest is private to duplicate detection; persisted evidence hashes retain
    their existing representation.
    """
    arrays = [np.asarray(values, dtype="<f8")
              for values in (track.time, track.xyz, track.spline.c)]
    if not all(np.all(np.isfinite(values)) for values in arrays):
        # The previous allow_nan=False JSON signature also rejected this case.
        raise ValueError("Nonfinite flight geometry cannot establish an identity")
    digest = hashlib.sha256(json.dumps([track.id, [a.shape for a in arrays]],
                                      allow_nan=False).encode())
    for values in arrays:
        digest.update(values.tobytes(order="C"))
    return digest.hexdigest()


def _tracks(indexes, bounds, time_range, budget, *, observers=None, radius=None, c=343.0,
            overload_observers=None, nonjet_classes=None, selection_counts=None):
    tracks, identities, samples = [], {}, 0
    if nonjet_classes is None:
        nonjet_classes = {}
    if selection_counts is None:
        selection_counts = {"aircraft_tracks_input": 0, "aircraft_known_jets_excluded": 0,
                            "aircraft_unknown_excluded": 0}
    queried, queried_samples = 0, 0

    def distant(track):
        try:
            return track.outside_observer_radius(observers, radius, c)
        except AcousticSupportError:
            # Keep unsupported tracks so the ordinary scorer records their
            # missing support, even when they appear distant.
            return False

    for index in indexes:
        for track in index.query(bounds, time_range):
            if nonjet_classes is not None:
                from .aircraft_types import track_class
                label = track_class(track, nonjet_classes)
                selection_counts["aircraft_tracks_input"] += 1
                if label not in ("piston", "turbine"):
                    key = "aircraft_known_jets_excluded" if label == "jet" else "aircraft_unknown_excluded"
                    selection_counts[key] += 1
                    continue
            if observers is not None or overload_observers is not None:
                queried += 1
                queried_samples += len(track.time)
                # The distance sieve may inspect extra distant tracks, but it
                # must retain the same bounded scan as the former retry.
                if observers is not None and (queried > 20 * budget.max_tracks_per_tile
                        or queried_samples > 20 * budget.max_track_samples_per_tile):
                    raise DataUnavailable("Tile track/sample memory budget exceeded; reduce tile_cells")
            # Same track from overlapping daily/source coverage must be identical;
            # silently dropping different geometry would change the physics.
            if track.id in identities:
                previous = identities[track.id]
                previous_signature = previous if isinstance(previous, str) else _track_geometry_digest(previous)
                if _track_geometry_digest(track) != previous_signature:
                    raise DataUnavailable(f"Conflicting flight segment identity: {track.id}")
                continue
            if observers is not None and distant(track):
                identities[track.id] = _track_geometry_digest(track)
                continue
            identities[track.id] = track
            tracks.append(track)
            samples += len(track.time)
            if len(tracks) > budget.max_tracks_per_tile or samples > budget.max_track_samples_per_tile:
                if observers is None and overload_observers is not None:
                    # Reuse the already decoded prefix and continue this query.
                    # Sampling terrain remains lazy for ordinary and empty tiles.
                    observers = overload_observers()
                    if (queried > 20 * budget.max_tracks_per_tile
                            or queried_samples > 20 * budget.max_track_samples_per_tile):
                        raise DataUnavailable("Tile track/sample memory budget exceeded; reduce tile_cells")
                    retained, samples = [], 0
                    for buffered in tracks:
                        if distant(buffered):
                            identities[buffered.id] = _track_geometry_digest(buffered)
                        else:
                            retained.append(buffered)
                            samples += len(buffered.time)
                    tracks = retained
                    del buffered
                if len(tracks) > budget.max_tracks_per_tile or samples > budget.max_track_samples_per_tile:
                    raise DataUnavailable("Tile track/sample memory budget exceeded; reduce tile_cells")
    return tracks


def _tracks_for_points(indexes, bounds, time_range, budget, points, terrain, radius, c, filters=None):
    """Switch overloaded queries to the conservative sieve without rereading."""
    def overload_observers():
        nonlocal points
        if "elev_m" not in points:
            points = _sample_tile_terrain(points, terrain)
        return ecef(points.lat.to_numpy(), points.lon.to_numpy(), points.elev_m.to_numpy())

    selection = {"aircraft_tracks_input": 0, "aircraft_known_jets_excluded": 0,
                 "aircraft_unknown_excluded": 0}
    classes = (filters or {}).get("_aircraft_type_classes", {})
    tracks = _tracks(indexes, bounds, time_range, budget, radius=radius, c=c,
                     overload_observers=overload_observers, nonjet_classes=classes,
                     selection_counts=selection)
    points.attrs["aircraft_selection_counts"] = selection
    return tracks, points


def _association_clock_window(start_window, sweep_s):
    """Enclose every association clock that could reach the scoring window."""
    lower, upper = map(float, start_window)
    if not all(math.isfinite(v) for v in (lower, upper, sweep_s)) or lower > upper or sweep_s < 0:
        raise ValueError("Recording clock bounds and sweep must be finite and ordered")
    # Association intervals are expanded by the scorer before intersection with
    # the requested window. Prune only outside that expanded envelope. Round
    # both the envelope and its center/radius representation outward at Unix
    # timestamp precision, so an inclusive endpoint cannot disappear.
    lower = math.nextafter(lower - sweep_s, -math.inf)
    upper = math.nextafter(upper + sweep_s, math.inf)
    center = lower + (upper - lower) / 2
    tolerance = math.nextafter(max(center - lower, upper - center), math.inf)
    return center, tolerance


def _event_receive_time_bounds(events, parameters, filters, start_window):
    """Conservatively bound matchable CPA arrivals for the shared-clock path.

    None means that this optional exclusion is not justified. Silence filters
    can use passages that match no event, so they cannot use this bound.
    """
    if (not start_window or parameters.get("mode") != "robust_ridge"
            or parameters.get("time_origin") is not None
            or filters.get("association", "subsets") != "subsets" or filters.get("silence")):
        return None
    try:
        c = float(parameters.get("c", 343.0))
        offset = float(filters.get("offset_tolerance_s", 5.0))
        if not math.isfinite(c) or c <= 0 or not math.isfinite(offset) or offset < 0:
            return None
        center, clock_tolerance = _association_clock_window(start_window, parameters.get("time_sweep_s", 5.0))
        lower = math.nextafter(center - clock_tolerance, -math.inf)
        upper = math.nextafter(center + clock_tolerance, math.inf)
        earliest, latest = [], []
        for event in events:
            cpa = float(event["cpa_receive_s"])
            event_tolerance = float(event.get("quality", {}).get("cpa_tolerance_s", 0))
            if not math.isfinite(cpa) or not math.isfinite(event_tolerance) or event_tolerance < 0:
                return None
            margin = math.nextafter(event_tolerance + offset / 2, math.inf)
            first = math.nextafter(math.nextafter(lower - margin, -math.inf) + cpa, -math.inf)
            bound = math.nextafter(math.nextafter(upper + margin, math.inf) + cpa, math.inf)
            # Association calculates receive_time - cpa before comparing clock
            # intervals. Extra ULPs cover that inverse floating-point rounding.
            bound += 4 * max(math.ulp(bound), math.ulp(center), math.ulp(cpa))
            first -= 4 * max(math.ulp(first), math.ulp(center), math.ulp(cpa))
            if not math.isfinite(bound) or not math.isfinite(first):
                return None
            earliest.append(first)
            latest.append(bound)
        return (min(earliest), max(latest)) if latest else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _tile_anchor_window(tile_id, start_window, parameters, filters, events):
    """Recover a deterministic half-open anchor partition from its tile ID."""
    path = [part for part in tile_id.split("~")[1:] if part.startswith("t")]
    if not path:
        return None
    if (len(path) > 24 or any(part not in ("t0", "t1") for part in path)
            or start_window is None or parameters.get("time_origin") is not None
            or parameters.get("mode") != "robust_ridge" or filters.get("association") != "subsets"):
        raise ValueError("Invalid association-clock partition")
    center, tolerance = _association_clock_window(start_window, parameters.get("time_sweep_s", 5.0))
    margins = [event.get("quality", {}).get("cpa_tolerance_s", 0)
               + filters.get("offset_tolerance_s", 5.0) / 2 for event in events]
    if not margins or any(not math.isfinite(v) or v < 0 for v in margins):
        raise ValueError("Invalid association-clock uncertainty")
    margin = max(margins)
    lo = math.nextafter(math.nextafter(center - tolerance, -math.inf) - margin, -math.inf)
    hi = math.nextafter(math.nextafter(center + tolerance, math.inf) + margin, math.inf)
    for part in path:
        mid = lo + (hi - lo) / 2
        if not lo < mid < hi:
            raise ValueError("Association-clock partition has no representable midpoint")
        if part == "t0":
            hi = mid
        else:
            lo = mid
    return lo, hi


def _partition_query_time_range(time_range, events, parameters, filters, anchor_window):
    """Bound whole-track queries by an association partition, not a clock prior.

    The first matched passage has an implied start inside ``anchor_window``.
    Every later matched passage must overlap its clock interval. Expanding by
    both passages' tolerances therefore contains every participating receive
    time. Subtract the largest possible acoustic delay to bound emission time.
    FlightIndex uses these bounds only to select complete original segments.
    """
    if (anchor_window is None or parameters.get("mode") != "robust_ridge"
            or filters.get("association", "subsets") != "subsets" or filters.get("silence")):
        return time_range

    def finite(value):
        # Keep unusual scalar/container semantics on the established path.
        if type(value) not in (int, float):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    if (not isinstance(anchor_window, (tuple, list)) or len(anchor_window) != 2
            or not all(finite(value) for value in anchor_window)
            or anchor_window[0] >= anchor_window[1]
            or not isinstance(time_range, (tuple, list)) or len(time_range) != 2
            or not all(finite(value) for value in time_range) or time_range[0] > time_range[1]
            or not isinstance(events, (tuple, list)) or not events):
        return time_range
    c = parameters.get("c", 343.0)
    offset = filters.get("offset_tolerance_s", 5.0)
    if not finite(c) or c <= 0 or not finite(offset) or offset < 0:
        return time_range
    tolerances, centers, radii = [], [], []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("quality", {}), dict):
            return time_range
        quality = event.get("quality", {})
        proposal_tolerances = [quality.get(name, 0) for name in
                               ("cpa_tolerance_s", "speed_tolerance_mps", "slant_tolerance_m")]
        # Do not let an empty narrowed query hide a filter_event validation error.
        if not all(finite(value) and value >= 0 for value in proposal_tolerances):
            return time_range
        slant = event.get("slant_range_m")
        speed = event.get("speed_range_mps")
        center = event.get("cpa_receive_s")
        if (not isinstance(slant, (tuple, list)) or len(slant) != 2
                or not all(finite(value) for value in slant) or not 0 <= slant[0] <= slant[1]
                or not isinstance(speed, (tuple, list)) or len(speed) != 2
                or not all(finite(value) for value in speed) or not 0 <= speed[0] <= speed[1]
                or not finite(center)):
            return time_range
        tolerance = proposal_tolerances[0] + offset / 2
        if not finite(tolerance):
            return time_range
        tolerances.append(tolerance)
        centers.append(center)
        radii.append(slant[1])
    if max(radii) <= 0:
        return time_range

    down = lambda value: math.nextafter(value, -math.inf)
    up = lambda value: math.nextafter(value, math.inf)
    # Any event can be the first matched one after unmatched proposals. Use
    # the maximum tolerance over all events, not the first event in the list.
    margin = max(tolerances)
    start_lo, start_hi = down(anchor_window[0] - margin), up(anchor_window[1] + margin)
    receive_lo, receive_hi = math.inf, -math.inf
    for center, tolerance in zip(centers, tolerances):
        implied_lo = down(down(start_lo) - tolerance)
        implied_hi = up(up(start_hi) + tolerance)
        receive_lo = min(receive_lo, down(down(implied_lo) + center))
        receive_hi = max(receive_hi, up(up(implied_hi) + center))
    # Extra outward steps before inverse operations cover rounded start =
    # receive - event CPA and receive = emission + distance / c arithmetic.
    delay = up(max(radii) / c)
    lower = down(down(receive_lo) - delay)
    upper = up(receive_hi)
    if not all(math.isfinite(value) for value in (start_lo, start_hi, receive_lo, receive_hi, delay, lower, upper)):
        return time_range
    lower, upper = max(time_range[0], lower), min(time_range[1], upper)
    # A segment can span two disjoint query windows. Keep the original path
    # rather than treating their empty intersection as no matching segments.
    return (lower, upper) if lower <= upper else time_range


def _clock_children(tile_id, cells, start_window, parameters, filters, events):
    if len([p for p in tile_id.split("~")[1:] if p.startswith("t")]) >= 24:
        return []
    children = [[f"{tile_id}~t{i}", list(cells)] for i in range(2)]
    try:
        for child, _ in children:
            _tile_anchor_window(child, start_window, parameters, filters, events)
    except ValueError:
        return []
    return children


def _track_cpa_batches(prepared, observers, point_ids, radius, step_s, chunk_points, max_rows, receive_bounds=None):
    batches, count = [], 0
    for first in range(0, len(observers), chunk_points):
        rows = _ordered_cpa_rows(prepared.raw_rows(observers[first:first + chunk_points],
            point_ids[first:first + chunk_points], radius, receive_time_bounds=receive_bounds), step_s)
        count += len(rows)
        if count > max_rows:
            raise DataUnavailable("Tile CPA-row memory budget exceeded")
        if rows:
            batches.append(np.array(rows, dtype=_CPA_DTYPE))
    return batches, count


@_cache_ridge_fits
def _score(points, tracks, events, measurements, budget, parameters, filters, start_window=None, *, ledger=None, stage=None, score_floor=None, anchor_window=None):
    radius = max(e["slant_range_m"][1] for e in events)
    from .aircraft_types import track_class
    classes = filters.get("_aircraft_type_classes", {})
    labels = [track_class(track, classes) for track in tracks]
    selection = points.attrs.get("aircraft_selection_counts")
    type_counts = dict(selection) if selection is not None else {
        "aircraft_tracks_input": len(tracks),
        "aircraft_known_jets_excluded": sum(label == "jet" for label in labels),
        "aircraft_unknown_excluded": sum(label not in ("jet", "piston", "turbine") for label in labels),
    }
    tracks = [track for track, label in zip(tracks, labels) if label in ("piston", "turbine")]
    observers = ecef(points.lat.to_numpy(), points.lon.to_numpy(), points.elev_m.to_numpy())
    point_ids = points.point_id.to_numpy(dtype=int)
    cpa_batches, count = [], 0
    unsupported, clock_excluded, interval_supported = [], [], []
    receive_bounds = _event_receive_time_bounds(events, parameters, filters, start_window)
    # Bound materialized CPA rows before pairing; one track at a time keeps the
    # national traffic count out of the CPA tensor/list allocation.
    for track in tracks:
        samples = math.ceil((track.time[-1] - track.time[0]) / budget.cpa_step_s) + 1
        if samples > budget.max_track_samples_per_tile:
            raise DataUnavailable("CPA time-sampling memory budget exceeded")
        # Even a pathological looping track cannot allocate an unbounded list
        # of minima before we inspect the returned table.
        chunk_points = max(1, budget.max_cpa_rows // samples)
        track_batches, track_rows = [], 0
        prepared = None
        try:
            if track.outside_observer_radius(observers, radius, parameters.get("c", 343.0)):
                continue
            prepared = _PreparedCPATrack(track, parameters.get("c", 343.0), budget.cpa_step_s)
            track_batches, track_rows = _track_cpa_batches(prepared, observers, point_ids, radius,
                budget.cpa_step_s, chunk_points, budget.max_cpa_rows-count, receive_bounds)
        except AcousticSupportError as error:
            if receive_bounds is not None and track.time[0] > receive_bounds[1]:
                clock_excluded.append({"track_id": track.id, "reason": error.reason,
                                       "reported_intervals": _support_error(error),
                                       "earliest_emission_s": float(track.time[0]),
                                       "latest_event_receive_s": receive_bounds[1]})
                continue
            if receive_bounds is not None:
                c = float(parameters.get("c", 343.0))
                delay = math.nextafter(radius / c, math.inf)
                last_receive = math.nextafter(float(track.time[-1]) + delay, math.inf)
                last_receive += 4 * max(math.ulp(last_receive), math.ulp(float(track.time[-1])), math.ulp(delay))
                if math.isfinite(last_receive) and last_receive < receive_bounds[0]:
                    clock_excluded.append({"track_id": track.id, "reason": error.reason,
                                           "reported_intervals": _support_error(error),
                                           "exclusion_kind": "before_events",
                                           "latest_emission_s": float(track.time[-1]),
                                           "latest_possible_receive_s": last_receive,
                                           "earliest_event_receive_s": receive_bounds[0]})
                    continue
            if error.reason == "non_subsonic_motion":
                try:
                    prepared = _PreparedCPATrack(track, parameters.get("c", 343.0), budget.cpa_step_s,
                                                motion_observers=observers, max_distance=radius,
                                                receive_time_bounds=receive_bounds)
                    if prepared.motion_support is None:
                        raise error
                    track_batches, track_rows = _track_cpa_batches(prepared, observers, point_ids, radius,
                        budget.cpa_step_s, chunk_points, budget.max_cpa_rows-count, receive_bounds)
                except AcousticSupportError as local_error:
                    error = local_error
                else:
                    interval_supported.append(prepared.motion_support)
                    count += track_rows
                    cpa_batches.extend(track_batches)
                    continue
            # A failed interval proof still loses the whole track/stage. Direct
            # acoustic scoring separately checks arrivals from every original
            # invalid interval, including intervals proved irrelevant to CPA.
            unsupported.append({"track_id": track.id, "reported_track_id": error.track_id, "reason": error.reason,
                "reported_intervals": _support_error(error),
                "emission_time_range_s": [float(track.time[0]), float(track.time[-1])],
                "receiver_bounds_wsen": [float(points.lon.min()), float(points.lat.min()),
                                         float(points.lon.max()), float(points.lat.max())],
                "receiver_point_count": len(points), "coverage_envelope": "whole_track_whole_stage_conservative"})
            continue
        finally:
            prepared = None
        count += track_rows
        cpa_batches.extend(track_batches)
    if not cpa_batches:
        if ledger is not None:
            ledger.record(stage, [], [], 0)
        return [], {**type_counts, "cpa_rows": 0, "candidates": 0, "unsupported_cpa_tracks": unsupported,
                    "clock_excluded_unsupported_tracks": clock_excluded,
                    "interval_supported_cpa_tracks": interval_supported}
    if parameters.get("mode") == "robust_ridge":
        from .matching import _prepare_score_inputs
        events, measurements = _prepare_score_inputs(events, measurements)
    table = pd.DataFrame({name: np.concatenate([batch[name] for batch in cpa_batches])
                          for name in _CPA_DTYPE.names}, copy=False)
    del cpa_batches, track_batches
    kwargs = dict(offset_tolerance_s=filters.get("offset_tolerance_s", 5.0),
                  time_origin=parameters.get("time_origin"), time_tolerance_s=filters.get("time_tolerance_s", 5.0),
                  silence=filters.get("silence"))
    subset = filters.get("association", "subsets" if parameters.get("mode") == "robust_ridge" else "full") == "subsets"
    if subset:
        from .matching import association_candidates
        if start_window is not None and kwargs["time_origin"] is None:
            kwargs["time_origin"], kwargs["time_tolerance_s"] = _association_clock_window(
                start_window, parameters.get("time_sweep_s", 5.0))
        pair_screen = None
        fixed_clock = (parameters.get("mode") == "robust_ridge" and parameters.get("time_origin") is not None
                       and parameters.get("time_sweep_s", 5.0) == 0)
        if fixed_clock:
            from .matching import _frequency_scale, association_score, score_ridges
            from .search import _ridge_trial_classification
            screen_budget = budget.max_candidates_per_tile
            screen_counts = {"checked": 0, "rejected": 0, "unknown": 0,
                             "budget_exhausted": False, "rejections": []}

            def pair_screen(event_index, event, frame):
                keep = np.ones(len(frame), dtype=bool)
                initial_checked = screen_counts["checked"]
                initial_rejected = screen_counts["rejected"]
                initial_unknown = screen_counts["unknown"]
                rejection_start = len(screen_counts["rejections"])
                exhausted_before = screen_counts["budget_exhausted"]
                for (point_id, flight_id), indices in frame.groupby(["point_id", "flight_id"], sort=False).groups.items():
                    if screen_counts["checked"] >= screen_budget:
                        screen_counts["budget_exhausted"] = True
                        break
                    screen_counts["checked"] += 1
                    point = lookup.get(point_id)
                    track = track_lookup.get(flight_id)
                    if point is None or track is None:
                        screen_counts["unknown"] += 1
                        continue
                    try:
                        result = score_ridges([track], [event], [measurements[event_index]],
                                              ecef(point.lat, point.lon, point.elev_m), kwargs["time_origin"],
                                              parameters.get("c", 343.0))
                        result = association_score(result, [event])
                        classification, reason = _ridge_trial_classification(result, [event], True)
                    except AcousticSupportError:
                        classification, reason, result = "unknown", "unsupported_acoustic_model", {}
                    if classification == "rejected":
                        positions = frame.index.get_indexer_for(indices)
                        keep[positions] = False
                        screen_counts["rejected"] += 1
                        diagnostics = result.get("event_diagnostics", [{}])
                        score = result.get("event_scores", [None])[0]
                        limit = 3 * _frequency_scale(event)
                        screen_counts["rejections"].append({
                            "event_index": event_index, "event_id": event.get("event_id", str(event_index)),
                            "point_id": int(point_id), "flight_id": str(flight_id),
                            "recording_start_time_s": float(kwargs["time_origin"]),
                            "event_score_hz": float(score), "residual_limit_hz": float(limit),
                            "support_fraction": diagnostics[0].get("support_fraction"), "reason": reason})
                    elif classification == "unknown":
                        screen_counts["unknown"] += 1
                return frame.loc[keep], {
                    "checked": screen_counts["checked"] - initial_checked,
                    "rejected": screen_counts["rejected"] - initial_rejected,
                    "unknown": screen_counts["unknown"] - initial_unknown,
                    "budget_exhausted": screen_counts["budget_exhausted"] and not exhausted_before,
                    "rejections": list(screen_counts["rejections"][rejection_start:]),
                }

            # CPA rows are created below, so resolve receiver/track identities
            # after all rows are available but before association enumeration.
            lookup = {row.point_id: row for row in points.itertuples()}
            track_lookup = {track.id: track for track in tracks}
        candidates, counts = association_candidates(table, events, **kwargs,
            min_events=filters.get("min_events", 1), max_events=filters.get("max_events") or len(events),
            max_candidates=budget.max_candidates_per_tile, pair_screen=pair_screen, anchor_window=anchor_window)
        if pair_screen is not None:
            counts["fixed_clock_event_pair_screen"] = {"recording_start_time_s": float(kwargs["time_origin"]),
                **screen_counts, "rejections": list(screen_counts["rejections"])}
    else:
        # Count the same inclusive clock-boundary passages as pair_candidates.
        per_event = [filter_association_event(table, e, kwargs["offset_tolerance_s"],
                    kwargs["time_origin"], kwargs["time_tolerance_s"]).groupby("point_id").size().to_dict()
                     for e in events]
        upper = sum(math.prod(g.get(p, 0) for g in per_event) for p in points.point_id)
        if upper > budget.max_candidates_per_tile:
            raise DataUnavailable("Tile candidate memory budget exceeded; reduce tile_cells")
        candidates, counts = pair_candidates(table, events, **kwargs)
    screened = subset and parameters.get("mode") == "robust_ridge"
    # Sparse tiles showed no benefit from caching batched propagation. Keep
    # the exact-input cache for dense association sets, where repetition can
    # outweigh key and copy costs, and preserve the ordinary path elsewhere.
    if not screened or len(candidates) < 1000:
        from .matching import _ridge_fit_cache
        _ridge_fit_cache.get().acoustic_limit = 0
    # Every accepted association has a nonnegative score, even before K modes
    # exist. A strict zero floor can reject only residual-incompatible trials.
    cutoff = _RetainedScoreFloor(budget.top_k, 0.0 if score_floor is None else max(0.0, score_floor)) if screened else None
    if screened:
        # Find competitive multi-group modes before spending fits on singletons.
        # All remaining modes still receive a score or a persisted score bound.
        candidates.sort(key=lambda candidate: (candidate["point_id"], -len(candidate["event_indices"])))
    # Attach approach identities before scoring, since legacy rank drops passes.
    ranked, unclassified, rejected, retention_pruned = [], 0, 0, 0
    bounds = []
    trial_counts = dict(evaluated_trials=0, rejected_trials=0, unknown_trials=0, unreported_trial_envelopes=0)
    unclassified_assessments = []
    lookup = {row.point_id: row for row in points.itertuples()}
    track_lookup = {track.id: track for track in tracks}
    for candidate in candidates:
        settings = dict(parameters)
        scoring_candidate = candidate
        if start_window:
            if start_window[0] == start_window[1] and settings.get("time_origin") is None:
                settings["time_origin"] = start_window[0]
            anchor = settings.get("time_origin")
            if anchor is None:
                anchor_event = candidate.get("event_indices", [0])[0]
                anchor = candidate["passes"][0]["receive_time"] - events[anchor_event]["cpa_receive_s"]
            sweep = settings.get("time_sweep_s", 5.0)
            lower, upper = candidate.get("recording_start_bounds_s", (anchor, anchor))
            lo, hi = max(lower - sweep, start_window[0]), min(upper + sweep, start_window[1])
            if settings.get("time_origin") is not None:
                lo, hi = max(lo, anchor-sweep), min(hi, anchor+sweep)
            if lo > hi:
                continue
            if "event_indices" in candidate:
                scoring_candidate = dict(candidate, recording_start_bounds_s=[lo, hi])
                settings.update(time_origin=None, time_sweep_s=0)
            else:
                settings.update(time_origin=(lo + hi) / 2, time_sweep_s=(hi - lo) / 2)
        point = lookup[candidate["point_id"]]
        if cutoff is not None and cutoff.value is not None:
            settings["score_floor"] = cutoff.value
        try:
            row = score_candidate(scoring_candidate, point, track_lookup, events, measurements, **settings)
        except AcousticSupportError as error:
            # Legacy scorers may stop before reporting their trial counts. Do
            # not invent an exact count for an interrupted clock envelope.
            row = dict(score=-np.inf, **{k: float(getattr(point, k)) for k in ("x", "y", "lat", "lon", "elev_m")},
                       flight_ids=[p["flight_id"] for p in candidate["passes"]],
                       event_indices=candidate.get("event_indices", list(range(len(events)))),
                       support_failure=_support_error(error))
        row["pass_emission_times_s"] = [float(p["emission_time"]) for p in candidate["passes"]]
        finite_score = row["score"] is not None and math.isfinite(row["score"])
        known = (classified_assessment(row) if settings.get("mode") == "robust_ridge" else
                 settings.get("time_origin") is not None and settings.get("time_sweep_s", 5) == 0 and finite_score)
        row["core_assessment_complete"] = bool(known and finite_score)
        if settings.get("mode") == "robust_ridge":
            assessment = row.get("scoring_assessment")
            if (isinstance(assessment, dict)
                    and assessment.get("unknown_score_bound_method") == "group_count_ceiling"
                    and "matched_group_count_below_score_floor" in assessment.get("reasons", ())):
                assessment["retention_score_floor"] = settings["score_floor"]
            if isinstance(assessment, dict) and all(type(assessment.get(k)) is int and assessment[k] >= 0
                                                  for k in ("evaluated_trials", "rejected_trials", "unknown_trials")):
                for key in ("evaluated_trials", "rejected_trials", "unknown_trials"):
                    trial_counts[key] += assessment[key]
            else:
                trial_counts["unreported_trial_envelopes"] += 1
        elif not known:
            trial_counts["unreported_trial_envelopes"] += 1
        if not known:
            unclassified += 1
            detail = {"point_id": int(candidate["point_id"]), "envelope_index": unclassified,
                "score_kind": row.get("score_kind"),
                "receiver": {k: float(row[k]) for k in ("x", "y", "lat", "lon", "elev_m")},
                "event_indices": row.get("event_indices", list(range(len(events)))), "flight_ids": row["flight_ids"],
                "pass_emission_times_s": row["pass_emission_times_s"],
                "recording_start_bounds_s": scoring_candidate.get("recording_start_bounds_s"),
                "clock_settings": {k: settings.get(k) for k in ("time_origin", "time_sweep_s", "time_step_s")},
                "scoring_assessment": row.get("scoring_assessment"), "support_failure": row.get("support_failure"),
                "finite_best": finite_score}
            unclassified_assessments.append(detail)
            if bounded_assessment(row):
                bounds.append({"bound_id": "bound-" + content_hash(detail), "detail": detail,
                               "groups_count": len(detail["event_indices"]),
                               "upper_score": detail["scoring_assessment"]["unknown_score_upper_bound"]})
                retention_pruned += "retention_score_floor" in detail["scoring_assessment"]
        if finite_score:
            ranked.append(row)
            if cutoff is not None:
                cutoff.observe(row)
        elif known:
            rejected += 1
    kept = merge_candidates(ranked, budget.top_k, ledger=ledger, stage=stage, bounds=bounds)
    return kept, dict(counts, **type_counts, cpa_rows=count, candidates=len(candidates), unclassified_score_candidates=unclassified,
                      retention_score_pruned_candidates=retention_pruned,
                      bounded_score_candidates=len(bounds), unbounded_score_candidates=unclassified-len(bounds),
                      evaluated_rejections=rejected, unsupported_cpa_tracks=unsupported,
                      clock_excluded_unsupported_tracks=clock_excluded,
                      interval_supported_cpa_tracks=interval_supported,
                      unclassified_candidate_assessments=unclassified_assessments, **trial_counts)


def _record_support(output, stage, counts, summary):
    for name in ("aircraft_tracks_input", "aircraft_known_jets_excluded", "aircraft_unknown_excluded"):
        summary[name] = summary.get(name, 0) + counts.get(name, 0)
    summary["unsupported_cpa_track_evaluations"] += len(counts.get("unsupported_cpa_tracks", []))
    clock_excluded = len(counts.get("clock_excluded_unsupported_tracks", []))
    if clock_excluded:
        summary["clock_excluded_unsupported_track_evaluations"] = (
            summary.get("clock_excluded_unsupported_track_evaluations", 0) + clock_excluded)
    interval_supported = len(counts.get("interval_supported_cpa_tracks", []))
    if interval_supported:
        summary["interval_supported_cpa_track_evaluations"] = (
            summary.get("interval_supported_cpa_track_evaluations", 0) + interval_supported)
    for key in ("unclassified_score_candidates", "bounded_score_candidates", "unbounded_score_candidates",
                "evaluated_trials", "rejected_trials", "unknown_trials", "unreported_trial_envelopes"):
        summary[key] += counts.get(key, 0)
    pair_screen = counts.get("fixed_clock_event_pair_screen")
    if pair_screen:
        for key in ("checked", "rejected", "unknown"):
            summary[f"fixed_clock_event_pairs_{key}"] = (
                summary.get(f"fixed_clock_event_pairs_{key}", 0) + pair_screen[key])
        summary["fixed_clock_event_pair_screen_budget_exhausted"] = (
            summary.get("fixed_clock_event_pair_screen_budget_exhausted", False)
            or pair_screen["budget_exhausted"])
        (output / "support").mkdir(exist_ok=True)
        write_json(output / "support" / (stage.replace(":", "-") + "-event-pair-screen.json"),
                   {"stage": stage, **pair_screen})
    if (counts.get("unsupported_cpa_tracks") or counts.get("unclassified_candidate_assessments")
            or counts.get("clock_excluded_unsupported_tracks") or counts.get("interval_supported_cpa_tracks")):
        (output / "support").mkdir(exist_ok=True)
        write_json(output / "support" / (stage.replace(":", "-") + ".json"),
                   {"stage": stage, "unsupported_cpa_tracks": counts.get("unsupported_cpa_tracks", []),
                    "clock_excluded_unsupported_tracks": counts.get("clock_excluded_unsupported_tracks", []),
                    "interval_supported_cpa_tracks": counts.get("interval_supported_cpa_tracks", []),
                    "unclassified_candidate_assessments": counts.get("unclassified_candidate_assessments", [])},
                   compact=True)


def search_national(indexes, terrain, events, measurements, *, time_range_s, output_dir,
                    bounds=CONUS, budget=None, scoring=None, filters=None, crs="EPSG:5070", input_hashes=None,
                    workers=None, search_progress=None, receiver_mask=None):
    """Bounded outer driver using existing physical CPA/locked-event scoring.

    `events, measurements` come from `matching.prepare_events`. `time_range_s`
    is the absolute *emission* data query window, expanded for sound travel and
    the complete uncertain recording clock. `scoring.time_origin` is either a
    trusted fixed clock or None; it is never set independently per tile/event.
    A clock range can additionally constrain `recording_start_time_s` globally.
    """
    budget = budget or SearchBudget()
    if not indexes or not events or len(events) != len(measurements):
        raise ValueError("Supply flight indexes and one measurement per event")
    for e in events:
        if not 0 < e["slant_range_m"][1] < 1000000:
            raise ValueError("Event slant range must be finite and <1000 km")
    parameters = dict(scoring or {})
    start_window = parameters.pop("recording_start_window_s", None)
    if start_window and start_window[0] == start_window[1] and parameters.get("time_origin") is None:
        parameters["time_origin"] = start_window[0]
    filters = dict(filters or {})
    filters.setdefault("association", "subsets" if parameters.get("mode") == "robust_ridge" else "full")
    if filters["association"] not in ("subsets", "full"):
        raise ValueError("association must be 'subsets' or 'full'")
    if filters.get("aircraft_type", "confirmed_nonjet") != "confirmed_nonjet":
        raise ValueError("aircraft_type must be confirmed_nonjet")
    filters["aircraft_type"] = "confirmed_nonjet"
    type_database = filters.get("aircraft_type_database")
    if not type_database:
        for index in indexes:
            index_path = getattr(index, "path", None)
            if index_path is None:
                continue
            candidates = (parent / "icao_aircraft_types.js" for parent in Path(index_path).parents)
            type_database = next((str(path) for path in candidates if path.is_file()), None)
            if type_database:
                break
    if type_database:
        from .aircraft_types import load_classes
        classes, digest = load_classes(type_database)
        filters["aircraft_type_database"] = str(type_database)
        filters["_aircraft_type_classes"] = classes
        filters["aircraft_type_database_sha256"] = digest
    event_limit = filters.get("max_events")
    if event_limit is None:
        event_limit = len(events)
    if type(event_limit) is not int or event_limit < 1:
        raise ValueError("max_events must be a positive integer or None for all groups")
    if filters["association"] == "full":
        event_limit = len(events)
    event_limit_applied = filters["association"] == "subsets" and event_limit < len(events)
    import os
    from contextlib import ExitStack, nullcontext
    from .data_access import CachedTerrain
    from .terrain import Terrain
    from .parallel_retention import discard_retention_stage, import_retention_stage
    from .parallel_tiles import TileFailure, initialize_tile_worker, ordered_tile_results, tile_worker_lanes
    xmin, ymin, xmax, ymax = _grid_extent(bounds, budget.spacing_m, crs)
    total = (xmax // budget.tile_cells - xmin // budget.tile_cells + 1) * (ymax // budget.tile_cells - ymin // budget.tile_cells + 1)
    if budget.max_tiles is None:
        coarse_cells = (xmax-xmin+1) * (ymax-ymin+1)
    else:
        coarse_cells = sum((cells[2]-cells[0]) * (cells[3]-cells[1])
                           for _, cells in itertools.islice(
                               grid_tiles(bounds, budget.spacing_m, budget.tile_cells, crs), budget.max_tiles))
    last_progress = 0.0
    last_progress_report = time.monotonic()

    def report_search_progress(fraction, *, force=False):
        nonlocal last_progress, last_progress_report
        if search_progress is None:
            return
        fraction = min(1.0, max(0.0, float(fraction)))
        now = time.monotonic()
        if fraction > last_progress and (
            force or fraction - last_progress >= 0.025 or now - last_progress_report >= 15.0
        ):
            search_progress(fraction)
            last_progress = fraction
            last_progress_report = now

    available = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    if workers is None:
        workers = max(1, available - 1)
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    workers = min(workers, total, budget.max_tiles or total, budget.max_tile_evaluations or total)
    # GDAL virtual/in-memory datasets can be valid in the parent but cannot
    # necessarily be reopened in a spawned process. Preserve the serial path
    # for those inputs instead of turning a working search into a failed one.
    explicit_rasters = list(terrain.rasters) if type(terrain) is Terrain else []
    if type(terrain) is Terrain and terrain.geoid:
        explicit_rasters.extend(terrain.geoid.rasters)
    explicit_reopenable = (type(terrain) is Terrain and
                           all(Path(raster.name).is_file() for raster in explicit_rasters))
    parallel = workers > 1 and (type(terrain) is CachedTerrain or explicit_reopenable)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "manifest.json"
    floor_policy = "per_tile" if parallel else "global_and_per_tile"
    if not parallel and manifest.exists():
        previous = json.loads(manifest.read_text())
        if previous.get("configuration", {}).get("coarse_floor_policy") == "per_tile":
            floor_policy = "per_tile"
    # Include actual acoustic measurements and implementation files in resume
    # identity; a detector/scorer change must never reuse stale tile scores.
    acoustic = []
    for spec, ridge, weight, fit in measurements:
        acoustic.append([hashlib_array(a) for a in [spec.time, getattr(spec, "frequency", []),
                                                   getattr(spec, "contrast", []), ridge, weight]])
    from .provenance import implementation_files
    package_root = Path(__file__).parent
    modules = {p.relative_to(package_root).as_posix(): sha256(p)
               for p in implementation_files(package_root, recursive=False)}
    if hasattr(terrain, "files"):
        terrain_signature = {name: value["sha256"] for name, value in terrain.files.items()
                             if name in ("tileList.txt", terrain.geoid_path.name)}
    else:
        rasters = list(getattr(terrain, "rasters", []))
        if getattr(terrain, "geoid", None):
            rasters += terrain.geoid.rasters
        terrain_signature = {"constant_m": getattr(terrain, "constant", None),
                             "rasters": {str(r.name): sha256(r.name) for r in rasters}}
    runtime_identity = {"python": platform.python_version(), "machine": platform.machine(),
                        "packages": {name: version(name) for name in ("numpy", "scipy", "pandas", "pyproj", "rasterio", "pyarrow")}}
    if RIDGE_FIT_BACKEND != "python-irls-v1":
        runtime_identity["ridge_fit_backend"] = RIDGE_FIT_BACKEND
    if ACOUSTIC_BACKEND != "python-acoustic-v1":
        runtime_identity["acoustic_backend"] = ACOUSTIC_BACKEND
    if CPA_BACKEND != "python-cpa-v1":
        runtime_identity["cpa_backend"] = CPA_BACKEND
    identity = {"indexes": [{"input_hash": i.report["input_hash"], "file_sha256": cached_sha256(i.path)} for i in indexes],
                "terrain": terrain_signature, "additional_input_hashes": input_hashes or {},
                "events": events, "acoustic_hashes": acoustic, "bounds": list(bounds), "time_range_s": list(time_range_s),
                "budget": asdict(budget), "scoring": scoring or {}, "filters": filters, "crs": crs, "code": modules,
                "coarse_floor_policy": floor_policy,
                "runtime": runtime_identity}
    if receiver_mask is not None:
        identity["receiver_mask"] = receiver_mask.identity
        runtime_identity["packages"]["shapely"] = version("shapely")
    run_hash = content_hash(identity)
    if manifest.exists() and json.loads(manifest.read_text())["run_hash"] != run_hash:
        raise DataUnavailable("Checkpoint configuration/input/implementation mismatch; use a new output directory")
    write_json(manifest, {"run_hash": run_hash, "configuration": identity})
    from .checkpoint_resume import compatible_checkpoint_runs
    compatible_runs = compatible_checkpoint_runs(output, run_hash)
    terrain_hash_cache = OrderedDict()
    mode = parameters.get("mode", "brightness")
    score_kind = ("matched_group_support_minus_normalized_residual" if mode == "robust_ridge" and filters["association"] == "subsets"
                  else {"robust_ridge": "negative_robust_ridge_loss_hz", "ridge": "negative_ridge_rmse_hz",
                        "brightness": "summed_spectral_contrast"}[mode])
    ledger = RetentionLedger(output / "retention.sqlite", run_hash, score_kind)
    fine_manifest = output / "fine-checkpoint-config.json"
    fine_config = {"run_hash": run_hash, "workers": workers if parallel else 1}
    has_fine_checkpoints = any(output.glob("fine-tile-*.json"))
    fine_config_written = False
    if fine_manifest.exists():
        try:
            previous_fine_config = json.loads(fine_manifest.read_text())
        except (OSError, ValueError) as error:
            raise DataUnavailable("Corrupt fine-stage checkpoint configuration") from error
        if previous_fine_config != fine_config and has_fine_checkpoints:
            raise DataUnavailable("Fine-stage checkpoints require the same search worker count")
        fine_config_written = previous_fine_config == fine_config
    elif has_fine_checkpoints:
        raise DataUnavailable("Fine-stage checkpoint configuration is missing")
    began, ranked, failures, done, processed_cells, resumed, points_searched = time.monotonic(), [], [], 0, 0, 0, 0
    tile_evaluations_started, tile_budget_exhausted = 0, False
    counted_clock_cells = set()
    support = dict(unsupported_cpa_track_evaluations=0, unclassified_score_candidates=0,
                   bounded_score_candidates=0, unbounded_score_candidates=0,
                   evaluated_trials=0, rejected_trials=0, unknown_trials=0, unreported_trial_envelopes=0)
    radius = max(e["slant_range_m"][1] for e in events)

    def restore_terrain(provenance):
        if type(terrain) is CachedTerrain:
            for source in provenance.get("terrain_files", []):
                terrain.files[Path(source["path"]).name] = source
            terrain.ocean_tiles.update(provenance.get("terrain_ocean_tiles", []))
            terrain.failures.extend(provenance.get("terrain_failures", []))

    def fine_checkpoint(stage, cells, exclusions, score_floor, payload=None):
        """Persist or validate a complete fine-piece payload and its ledger."""
        nonlocal fine_config_written
        coordinates = "_".join(str(value) for value in cells)
        path = output / f"fine-tile-{stage.replace(':', '-')}-{coordinates}.json"
        parameters = {"stage": stage, "cells": list(cells),
                      "exclusions": [list(rectangle) for rectangle in exclusions], "score_floor": score_floor}
        if payload is not None:
            if not fine_config_written:
                write_json(fine_manifest, fine_config)
                fine_config_written = True
            record = {"run_hash": run_hash, "parameters": parameters,
                      "payload_hash": content_hash([parameters, payload]), "payload": payload}
            write_json(path, record, compact=True)
            return None
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text())
            saved = record["payload"]
            if (record["run_hash"] != run_hash
                    or record["payload_hash"] != content_hash([record["parameters"], saved])):
                raise DataUnavailable("Corrupt fine-tile checkpoint")
            if record["parameters"] != parameters:
                return None
            for source in saved.get("terrain_files", []):
                if _verified_terrain_hash(source["path"], terrain_hash_cache) != source["sha256"]:
                    raise DataUnavailable("Terrain changed since fine-tile checkpoint")
            return saved if ledger.stage_valid(stage, saved["retention_checksum"]) else None
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise DataUnavailable("Corrupt fine-tile checkpoint") from error

    def tile_request(tile_id, cells):
        stage = f"coarse:{tile_id}"
        checkpoint = output / f"tile-{tile_id}.json"
        cached = None
        try:
            if checkpoint.exists():
                record = json.loads(checkpoint.read_text())
                if record["run_hash"] not in compatible_runs or record["payload_hash"] != content_hash(record["payload"]):
                    raise DataUnavailable("Corrupt tile checkpoint")
                prior = record["payload"]
                for source in prior.get("terrain_files", []):
                    if _verified_terrain_hash(source["path"], terrain_hash_cache) != source["sha256"]:
                        raise DataUnavailable("Terrain changed since tile checkpoint")
                if ledger.stage_valid(stage, prior.get("retention_checksum")):
                    cached = checkpoint
        except (DataUnavailable, ValueError, OSError) as error:
            cached = error
        return stage, cells, cached, None

    def requests_for_wave(wave, next_wave):
        nonlocal total, tile_evaluations_started, tile_budget_exhausted
        for tile_id, cells in wave:
            marker = output / f"split-{tile_id}.json"
            if not marker.exists():
                request = tile_request(tile_id, cells)
                if request[2] is None:
                    if (budget.max_tile_evaluations is not None
                            and tile_evaluations_started >= budget.max_tile_evaluations):
                        tile_budget_exhausted = True
                        continue
                    tile_evaluations_started += 1
                yield request
                continue
            try:
                record = json.loads(marker.read_text())
                if record.get("run_hash") in compatible_runs:
                    record["run_hash"] = run_hash
                parts = _split_tile_cells(cells)
                children = ([[f"{tile_id}~{i}", list(part)] for i, part in enumerate(parts)] if parts else
                            _clock_children(tile_id, cells, start_window, parameters, filters, events))
                if not children or record != {"run_hash": run_hash, "cells": list(cells), "children": children}:
                    raise DataUnavailable("Corrupt tile subdivision checkpoint")
            except (DataUnavailable, ValueError, OSError) as error:
                yield f"coarse:{tile_id}", cells, error, None
                continue
            total += len(children)-1
            next_wave.extend((child_id, tuple(child_cells)) for child_id, child_cells in children)

    def tile_order(item):
        root, *path = item[0].split("~")
        ix, iy = map(int, root.split("_"))
        return iy, ix, tuple(4 + int(p[1:]) if p.startswith("t") else int(p) for p in path)

    if parallel:
        if type(terrain) is CachedTerrain:
            terrain_kind, terrain_root = "cached", terrain.root
            terrain_options = {"offline": terrain.offline, "geoid_path": terrain.geoid_path,
                               "adopt_existing": terrain.adopt_existing}
            dem_keys = lambda cells: _tile_dem_keys(cells, budget.spacing_m, bounds, crs, terrain.catalog)
        else:
            terrain_kind, terrain_root = "explicit", None
            paths = [raster.name for raster in terrain.rasters]
            geoid_path = terrain.geoid.rasters[0].name if terrain.geoid else None
            terrain_options = {"paths": paths, "constant_m": terrain.constant, "geoid_path": geoid_path}
            dem_keys = lambda cells: tuple(Path(path).stem for path in paths)
        initargs = ([index.path for index in indexes], terrain_root, terrain_options, events, measurements,
                    bounds, time_range_s, budget, parameters, filters, start_window, crs,
                    output / ".tile-workers", run_hash, score_kind, terrain_kind)
        if receiver_mask is not None:
            initargs += (receiver_mask,)
        context = tile_worker_lanes(workers, initialize_tile_worker, initargs, dem_keys)
    else:
        context = nullcontext(None)
    def finish_tile(stage, cells, future):
        checkpoint = output / f"tile-{stage[len('coarse:'):]}.json"
        payload = None
        try:
            payload, worker_path, *_ = future.result()
            import_retention_stage(ledger, worker_path, stage, payload["retention_checksum"])
            write_json(checkpoint, {"run_hash": run_hash, "payload_hash": content_hash(payload), "payload": payload},
                       compact=True)
            discard_retention_stage(worker_path)
        except (DataUnavailable, ValueError, OSError) as error:
            if payload is not None and not isinstance(error, TileFailure):
                provenance = {key: payload.get(key, []) for key in
                              ("terrain_files", "terrain_ocean_tiles", "terrain_failures")}
                return TileFailure(str(error), provenance)
            return error
        advance_progress(stage, cells)
        return checkpoint

    def advance_progress(stage, cells):
        nonlocal processed_cells
        clock_depth = sum(part.startswith("t") for part in stage.split("~")[1:])
        processed_cells += (cells[2]-cells[0]) * (cells[3]-cells[1]) / (2 ** clock_depth)
        if coarse_cells:
            report_search_progress(0.8 * processed_cells / coarse_cells)

    with context as pool, ExitStack() as empty_batches:
        empty_batch_tiles = 0

        def flush_empty_tiles():
            nonlocal empty_batch_tiles
            try:
                empty_batches.close()
            finally:
                empty_batch_tiles = 0

        roots = grid_tiles(bounds, budget.spacing_m, budget.tile_cells, crs)
        wave = itertools.islice(roots, budget.max_tiles) if budget.max_tiles is not None else roots
        while wave is not None:
            next_wave = []
            requests = requests_for_wave(wave, next_wave)
            results = (ordered_tile_results(pool, requests, workers, finish_tile) if pool is not None else
                       ((stage, cells, cached, cached is not None) for stage, cells, cached, _ in requests))
            for stage, cells, prepared, was_cached in results:
                tile_id = stage[len("coarse:"):]
                checkpoint = output / f"tile-{tile_id}.json"
                terminal = True
                if prepared is not None:
                    flush_empty_tiles()
                try:
                    if isinstance(prepared, BaseException):
                        raise prepared
                    if prepared is not None:
                        payload = json.loads(prepared.read_text())["payload"]
                        if was_cached:
                            resumed += 1
                        restore_terrain(payload)
                    else:
                        points = _tile_coordinates(cells, budget.spacing_m, bounds, crs, receiver_mask)
                        tracks = []
                        anchor = _tile_anchor_window(tile_id, start_window, parameters, filters, events)
                        query_time = _partition_query_time_range(time_range_s, events, parameters, filters, anchor)
                        if len(points):
                            tracks, points = _tracks_for_points(
                                indexes, _expanded_bounds(cells, budget.spacing_m, radius, crs), query_time,
                                budget, points, terrain, radius, parameters.get("c", 343.0), filters)
                        if tracks and "elev_m" not in points:
                            points = _sample_tile_terrain(points, terrain)
                        sampled = "elev_m" in points
                        selection_counts = points.attrs.get("aircraft_selection_counts", {})
                        if not tracks:
                            points = pd.DataFrame()
                        if len(points):
                            flush_empty_tiles()
                            rows, counts = _score(points, tracks, events, measurements, budget, parameters, filters, start_window,
                                                  ledger=ledger, stage=stage,
                                                  anchor_window=anchor,
                                                  score_floor=(_retained_score_floor(ranked, budget.top_k)
                                                               if floor_policy == "global_and_per_tile" else None))
                        else:
                            rows, counts = [], selection_counts
                            # Keep all coarse/global stage records, but amortize
                            # durable commits across at most 32 empty tiles.
                            # A crash can leave uncommitted checkpoint files;
                            # tile_request rejects those through stage_valid.
                            if empty_batch_tiles == 0:
                                empty_batches.enter_context(ledger.batch())
                            empty_batch_tiles += 1
                            ledger.record(stage, [], [], 0)
                        used = getattr(terrain, "last_sample_files", set()) if sampled else set()
                        terrain_files = [terrain.files[name] for name in sorted(used)] if used else []
                        oceans = sorted(getattr(terrain, "last_sample_ocean_tiles", set())) if sampled else []
                        payload = {"ranked": rows, "counts": counts, "grid_points": len(points), "tracks": len(tracks),
                                   "cells": list(cells), "terrain_files": terrain_files, "terrain_ocean_tiles": oceans,
                                   "terrain_failures": [], "retention_checksum": ledger.checksum(stage)}
                        write_json(checkpoint, {"run_hash": run_hash, "payload_hash": content_hash(payload), "payload": payload},
                                   compact=True)
                    done += 1
                    if "~t" not in tile_id:
                        points_searched += payload["grid_points"]
                    else:
                        cell_owner = tile_id.split("~t", 1)[0]
                        if cell_owner not in counted_clock_cells:
                            points_searched += payload["grid_points"]
                            counted_clock_cells.add(cell_owner)
                    _record_support(output, stage, payload["counts"], support)
                    ranked = merge_candidates(itertools.chain(ranked, payload["ranked"]), budget.top_k,
                                              ledger=ledger, stage=f"global:{tile_id}")
                except (DataUnavailable, ValueError, OSError) as error:
                    if isinstance(error, TileFailure):
                        restore_terrain(error.provenance)
                    if _tile_capacity_error(error):
                        # Keep subdivision markers unchanged for existing resume
                        # readers; store the lost failure reason separately.
                        write_json(output / f"capacity-{tile_id}.json",
                                   {"run_hash": run_hash, "stage": stage,
                                    "cells": list(cells), "reason": str(error)})
                    parts = _split_tile_cells(cells) if _tile_capacity_error(error) else []
                    children = [[f"{tile_id}~{i}", list(part)] for i, part in enumerate(parts)]
                    if not children and _association_capacity_error(error):
                        children = _clock_children(tile_id, cells, start_window, parameters, filters, events)
                    if children:
                        terminal = False
                        write_json(output / f"split-{tile_id}.json",
                                   {"run_hash": run_hash, "cells": list(cells), "children": children})
                        total += len(children)-1
                        next_wave.extend((child_id, tuple(child_cells)) for child_id, child_cells in children)
                    else:
                        failures.append({"tile": tile_id, "reason": str(error)})
                if empty_batch_tiles >= 32:
                    flush_empty_tiles()
                if terminal and (pool is None or was_cached or isinstance(prepared, BaseException)):
                    advance_progress(stage, cells)
            wave = sorted(next_wave, key=tile_order) if next_wave else None
        flush_empty_tiles()
        report_search_progress(0.8, force=True)
    coarse = list(ranked)
    # Refine only global finalists; tile selection never controls refinement rank.
    refinement_failure = None
    refinement_points_searched, refinement_points_skipped = 0, 0
    fine_stages = 0
    fine_resumed = 0
    if ranked and budget.refine_spacing_m < budget.spacing_m:
        chosen, seen = [], set()
        for row in ranked:
            approach = (tuple(row["flight_ids"]), tuple(round(t) for t in row["pass_emission_times_s"]))
            if approach not in seen:
                chosen.append(row)
                seen.add(approach)
            if len(chosen) >= budget.refine_finalists:
                break
        try:
            fine = budget.refine_spacing_m
            width = math.ceil(budget.spacing_m / fine)
            # Adaptive subdivision preserves cell area, so this denominator stays valid.
            fine_work_total = len(chosen) * (2 * width + 1) ** 2
            fine_work_done = 0

            def advance_fine_progress(cells):
                nonlocal fine_work_done
                fine_work_done += (cells[2] - cells[0]) * (cells[3] - cells[1])
                fraction = 0.8 + 0.2 * fine_work_done / fine_work_total
                report_search_progress(fraction, force=True)

            completed_rectangles = []
            if parallel:
                from collections import deque
                from concurrent.futures import Future
                from .data_access import _index_pool
                from .parallel_tiles import discard_pending_fine_ledgers, score_fine_tile

                with _index_pool(workers, initialize_tile_worker, initargs) as fine_pool:
                    for seed_index, row in enumerate(chosen):
                        cx, cy = round(row["x"] / fine), round(row["y"] / fine)
                        pending = deque((x0, y0, min(cx + width + 1, x0 + budget.tile_cells),
                                         min(cy + width + 1, y0 + budget.tile_cells))
                                        for y0 in range(cy - width, cy + width + 1, budget.tile_cells)
                                        for x0 in range(cx - width, cx + width + 1, budget.tile_cells))
                        inflight = deque()
                        try:
                            while pending or inflight:
                                while pending and len(inflight) < workers:
                                    cells = pending.popleft()
                                    stage = f"fine:{seed_index}:{cells[0]}:{cells[1]}"
                                    # The currently retained floor is conservative for every
                                    # submitted piece, even if later merges raise it.
                                    floor = _retained_score_floor(ranked, budget.top_k)
                                    cached = fine_checkpoint(stage, cells, completed_rectangles, floor)
                                    if cached is None:
                                        future = fine_pool.submit(score_fine_tile, stage, cells, fine,
                                                                  tuple(completed_rectangles), floor)
                                    else:
                                        future = Future()
                                        future.set_result((cached, None))
                                    inflight.append((cells, stage, future, floor))
                                cells, stage, future, floor = inflight.popleft()
                                try:
                                    payload, worker_path = future.result()
                                except (DataUnavailable, ValueError, OSError) as error:
                                    if isinstance(error, TileFailure):
                                        restore_terrain(error.provenance)
                                    parts = _split_tile_cells(cells) if _tile_capacity_error(error) else []
                                    if not parts:
                                        raise
                                    pending.extendleft(reversed(parts))
                                    continue
                                restore_terrain(payload)
                                refinement_points_skipped += payload["skipped"]
                                if worker_path is None and "retention_checksum" not in payload:
                                    advance_fine_progress(cells)
                                    continue
                                if worker_path is not None:
                                    import_retention_stage(ledger, worker_path, stage, payload["retention_checksum"])
                                    discard_retention_stage(worker_path)
                                    fine_checkpoint(stage, cells, completed_rectangles, floor, payload)
                                else:
                                    fine_resumed += 1
                                _record_support(output, stage, payload["counts"], support)
                                fine_stages += 1
                                refinement_points_searched += payload["grid_points"]
                                ranked = merge_candidates(itertools.chain(ranked, payload["ranked"]), budget.top_k,
                                                          ledger=ledger, stage=f"fine-global:{seed_index}:{cells[0]}:{cells[1]}")
                                advance_fine_progress(cells)
                        finally:
                            discard_pending_fine_ledgers(inflight)
                        completed_rectangles.append((cx - width, cy - width, cx + width + 1, cy + width + 1))
            else:
                for seed_index, row in enumerate(chosen):
                    # Subdivide the refinement square as well, retaining hard grid limits.
                    cx, cy = round(row["x"] / fine), round(row["y"] / fine)
                    for y0 in range(cy - width, cy + width + 1, budget.tile_cells):
                        for x0 in range(cx - width, cx + width + 1, budget.tile_cells):
                            root = (x0, y0, min(cx + width + 1, x0 + budget.tile_cells),
                                    min(cy + width + 1, y0 + budget.tile_cells))
                            pending = [root]
                            while pending:
                                cells = pending.pop()
                                stage = f"fine:{seed_index}:{cells[0]}:{cells[1]}"
                                floor = _retained_score_floor(ranked, budget.top_k)
                                payload = fine_checkpoint(stage, cells, completed_rectangles, floor)
                                if payload is None:
                                    failures_before = len(getattr(terrain, "failures", ()))
                                    points = tile_points(cells, fine, bounds, terrain, crs, receiver_mask)
                                    keep = np.ones(len(points), dtype=bool)
                                    for left, bottom, right, top in completed_rectangles:
                                        keep &= ~((points.x >= left * fine) & (points.x < right * fine)
                                                  & (points.y >= bottom * fine) & (points.y < top * fine))
                                    skipped = int((~keep).sum())
                                    points = points[keep]
                                    if not len(points):
                                        refinement_points_skipped += skipped
                                        advance_fine_progress(cells)
                                        continue
                                    try:
                                        tracks, points = _tracks_for_points(
                                            indexes, _expanded_bounds(cells, fine, radius, crs), time_range_s,
                                            budget, points, terrain, radius, parameters.get("c", 343.0), filters)
                                        rows, counts = _score(points, tracks, events, measurements, budget, parameters,
                                                              filters, start_window, ledger=ledger, stage=stage,
                                                              score_floor=floor)
                                    except (DataUnavailable, ValueError, OSError) as error:
                                        parts = _split_tile_cells(cells) if _tile_capacity_error(error) else []
                                        if not parts:
                                            raise
                                        pending.extend(reversed(parts))
                                        continue
                                    files = getattr(terrain, "files", {})
                                    names = getattr(terrain, "last_sample_files", set())
                                    payload = {"ranked": rows, "counts": counts, "grid_points": len(points),
                                               "skipped": skipped, "retention_checksum": ledger.checksum(stage),
                                               "terrain_files": [files[name] for name in sorted(names) if name in files],
                                               "terrain_ocean_tiles": sorted(getattr(terrain, "last_sample_ocean_tiles", set())),
                                               "terrain_failures": getattr(terrain, "failures", [])[failures_before:]}
                                    fine_checkpoint(stage, cells, completed_rectangles, floor, payload)
                                else:
                                    fine_resumed += 1
                                    restore_terrain(payload)
                                refinement_points_skipped += payload["skipped"]
                                _record_support(output, stage, payload["counts"], support)
                                fine_stages += 1
                                refinement_points_searched += payload["grid_points"]
                                ranked = merge_candidates(itertools.chain(ranked, payload["ranked"]), budget.top_k,
                                                          ledger=ledger, stage=f"fine-global:{seed_index}:{cells[0]}:{cells[1]}")
                                advance_fine_progress(cells)
                    completed_rectangles.append((cx - width, cy - width, cx + width + 1, cy + width + 1))
        except (DataUnavailable, ValueError, OSError) as error:
            refinement_failure = str(error)
    coverage = [i.coverage_for(bounds, time_range_s) for i in indexes]
    # Aircraft outside the receiver rectangle can still be audible inside it.
    # Use a conservative geographic envelope for scope accounting as well as
    # expanded per-tile queries; country-cropped inputs have visible border gaps.
    angular = (1.6 * radius + 15000) / 110000
    lon_margin = angular / max(0.1, math.cos(math.radians(max(abs(bounds[1]), abs(bounds[3])))))
    flight_bounds = [max(-180, bounds[0] - lon_margin), max(-90, bounds[1] - angular),
                     min(180, bounds[2] + lon_margin), min(90, bounds[3] + angular)]
    # Sources partitioning time can jointly cover the request; union interval
    # coverage is checked only for all-type sources covering the whole spatial box.
    intervals = sorted(i.report["coverage"]["time_range_s"] for i in indexes if i.coverage_for(flight_bounds, i.report["coverage"]["time_range_s"])["scope_complete"])
    covered_until = time_range_s[0]
    for lo, hi in intervals:
        if lo <= covered_until:
            covered_until = max(covered_until, hi)
    data_complete = bool(covered_until >= time_range_s[1])
    model_complete = not (support["unsupported_cpa_track_evaluations"] or support["unclassified_score_candidates"])
    model_assessed = not (support["unsupported_cpa_track_evaluations"] or support["unbounded_score_candidates"]
                         or support["unreported_trial_envelopes"])
    model_assessed &= support["bounded_score_candidates"] == support["unclassified_score_candidates"]
    unclassified_only = (support.get("aircraft_unknown_excluded", 0) > 0 and
        support.get("aircraft_tracks_input", 0) <= support.get("aircraft_known_jets_excluded", 0)
        + support.get("aircraft_unknown_excluded", 0))
    complete = (done == total and not failures and not refinement_failure and data_complete
                and not event_limit_applied and model_assessed and not tile_budget_exhausted
                and not unclassified_only)
    finite = ledger.evidence(ranked, complete=complete, spacing_m=budget.spacing_m,
                             refine_spacing_m=min(budget.spacing_m, budget.refine_spacing_m), max_event_groups=len(events),
                             expected_stages={"coarse": done, "global": done, "fine": fine_stages, "fine-global": fine_stages})
    finite["limitations"].append("Only aircraft classified as piston or turbine were searched; jets and unknown types were excluded.")
    if unclassified_only:
        finite["limitations"].append("No confirmed non-jet tracks were selected; aircraft-type metadata may be missing.")
    if tile_budget_exhausted:
        finite["limitations"].append(
            "The per-invocation coarse tile-evaluation budget was exhausted; unvisited tiles remain.")
    if finite["bounded_unknown_envelopes"] != support["bounded_score_candidates"]:
        model_assessed = complete = False
        finite["complete"] = finite["pruning_accounted_for"] = False
    finite["unclassified_score_candidates"] = support["unclassified_score_candidates"]
    finite["unsupported_cpa_track_evaluations"] = support["unsupported_cpa_track_evaluations"]
    if not model_assessed:
        finite["best_discarded_score"] = None
        finite["discarded_bound_status"] = "unknown_model_or_trial_support"
        finite["limitations"].append("Some unclassified trials or unsupported CPA tracks remain without a score bound.")
    else:
        finite["discarded_bound_status"] = ("includes_bounded_unknown_trials" if support["bounded_score_candidates"]
                                             else "evaluated_scores")
    finite["optimizer_domain"] = {"clock_window_s": start_window, "time_origin_s": parameters.get("time_origin"),
        "time_step_s": parameters.get("time_step_s", .5), "declared_sweep_s": parameters.get("time_sweep_s", 5.),
        "clock_rule": "shared CPA-consistency interval plus declared sweep, intersected with trusted window",
        "event_settings": [{"event_index": i, "f0_range_hz": e["f0_range_hz"], "quality": e.get("quality", {}),
                            "source_drift_fraction": e.get("quality", {}).get("source_drift_fraction", .02)}
                           for i, e in enumerate(events)], "assumptions_manifest": "manifest.json",
        "run_hash": run_hash, "source_fit_code_sha256": modules.get("matching.py"), "score_code_sha256": modules["search.py"]}
    finite["limitations"].append("Clock/frequency/drift are optimized at each fixed receiver/pass-association seed; clock uniqueness is not established.")
    finite["ledger_artifact"] = "retention.sqlite"
    report = {"run_hash": run_hash, "status": "complete" if complete else "partial", "ranked": ranked,
              "aircraft_scope": "confirmed_nonjet",
              "coarse_ranked": coarse, "tiles_total": total, "tiles_completed": done, "tiles_resumed": resumed,
              "tile_failures": failures, "unsearched_tiles": total - done, "grid_points_searched": points_searched,
              "data_coverage": coverage, "data_scope_complete": data_complete, "refinement_failure": refinement_failure,
              "model_support_complete": model_complete, "model_support": dict(support, diagnostics_directory="support"),
              "aircraft_classification_incomplete": bool(unclassified_only),
              "model_assessment_complete": model_assessed,
              "refinement_points_searched": refinement_points_searched, "refinement_points_skipped": refinement_points_skipped,
              "refinement_stages_resumed": fine_resumed,
              "tile_work_budget": {"max_new_tile_evaluations_per_invocation": budget.max_tile_evaluations,
                                   "new_tile_evaluations_started": tile_evaluations_started,
                                   "exhausted": tile_budget_exhausted},
              "required_flight_bounds": flight_bounds,
              "candidate_retention_limit": budget.top_k, "alternative_modes_exhaustively_assessed": False,
              "finite_search_evidence": finite,
              "core_assessed_mode_ids": [row["mode_id"] for row in ranked if row.get("core_assessment_complete")],
              "event_groups_proposed": len(events), "association_event_limit": event_limit,
              "all_event_groups_considered": not event_limit_applied,
              "event_limit_reason": "Configured event cap prevents assessing every independent group" if event_limit_applied else None,
              "elapsed_s": time.monotonic() - began, "process_peak_rss_mib": process_peak_rss_kib() / 1024,
              "bounded_concurrency": workers if parallel else 1, "max_grid_points_per_tile": budget.tile_cells ** 2,
              "scoring_time_is_locked": True, "ground_truth_used_by_search": False, "scope": list(bounds)}
    if receiver_mask is not None:
        report["receiver_mask"] = receiver_mask.identity
        report["crs"] = crs
    write_json(output / "search-report.json", report)
    report_search_progress(1.0, force=True)
    return report


def hashlib_array(value):
    import hashlib
    array = np.asarray(value)
    return hashlib.sha256(str(array.shape).encode() + str(array.dtype).encode() + array.tobytes()).hexdigest()
