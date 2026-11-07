"""Driver regression with the real Colsto job, terrain and warmed flight indexes.

Set DOPPLERGEIST_REAL_WORK_JOB and DOPPLERGEIST_REAL_WORK_CONFIG to run.
No generated observations. Spatial splitting is disabled only in this software
check to exercise the atomic-cell clock fallback with the observed work error.
"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dopplergeist import tiled_search as tiled
from dopplergeist.data_access import CachedTerrain, FlightIndex
from dopplergeist.matching import prepare_events


@unittest.skipUnless(os.environ.get('DOPPLERGEIST_REAL_WORK_JOB')
                     and os.environ.get('DOPPLERGEIST_REAL_WORK_CONFIG'),
                     'Requires the real recorded job and warmed indexes')
class RealAssociationWorkPartitionTests(unittest.TestCase):
    def test_observed_storage_limit_creates_resumable_clock_children(self):
        job = Path(os.environ['DOPPLERGEIST_REAL_WORK_JOB'])
        operator = json.loads(Path(os.environ['DOPPLERGEIST_REAL_WORK_CONFIG']).read_text())
        config = json.loads((job / 'search/manifest.json').read_text())['configuration']
        events, measurements = prepare_events(json.loads((job / 'discovery/discovery.json').read_text())['events'])
        self.assertEqual(events, config['events'])
        self.assertEqual(len(events), 16)
        budget = tiled.SearchBudget(**{**config['budget'], 'max_tiles': 1, 'max_tile_evaluations': 1})
        indexes = [FlightIndex(path) for path in operator['flight_indexes']]
        terrain = CachedTerrain(Path(operator['cache_root']) / 'terrain', offline=True)
        actual_score = tiled._score
        seen = []

        def score(*args, **kwargs):
            anchor = kwargs.get('anchor_window')
            seen.append(anchor)
            if anchor is not None:
                # Stop after entering the real clock child; this is a software
                # restart check, not another geolocation/accuracy experiment.
                raise OSError('bounded replay reached clock child')
            return actual_score(*args, **kwargs)

        def run(directory):
            return tiled.search_national(indexes, terrain, events, measurements,
                time_range_s=config['time_range_s'], output_dir=directory,
                bounds=config['bounds'], budget=budget, scoring=config['scoring'],
                filters=config['filters'], crs=config['crs'], workers=1)

        with TemporaryDirectory(prefix='doppler-real-work-partition-') as directory, \
                patch.object(tiled, '_split_tile_cells', return_value=[]), \
                patch.object(tiled, '_score', side_effect=score):
            directory = Path(directory)
            initial = run(directory)
            markers = list(directory.glob('split-*.json'))
            self.assertEqual(len(markers), 1)
            marker = json.loads(markers[0].read_text())
            root = markers[0].stem.removeprefix('split-')
            self.assertEqual(marker['children'], [[root + '~t0', marker['cells']],
                                                  [root + '~t1', marker['cells']]])
            capacity = json.loads(next(directory.glob('capacity-*.json')).read_text())
            self.assertEqual(capacity['reason'],
                             'Association work/storage budget exceeded; search is incomplete')
            self.assertTrue(initial['tile_work_budget']['exhausted'])
            self.assertEqual(seen, [None])
            resumed = run(directory)
            self.assertEqual(len(seen), 2)
            self.assertIsNotNone(seen[-1])
            self.assertTrue(any(failure['reason'] == 'bounded replay reached clock child'
                                for failure in resumed['tile_failures']))
            self.assertFalse(any('Corrupt' in failure['reason'] for failure in resumed['tile_failures']))
            self.assertEqual(json.loads(markers[0].read_text()), marker)
            params = dict(config['scoring'])
            window = params.pop('recording_start_window_s')
            anchors = [tiled._tile_anchor_window(child, window, params, config['filters'], events)
                       for child, _ in marker['children']]
            self.assertEqual(anchors[0][1], anchors[1][0])
            center, tolerance = tiled._association_clock_window(window, params['time_sweep_s'])
            self.assertLessEqual(anchors[0][0], center - tolerance)
            self.assertGreaterEqual(anchors[1][1], center + tolerance)


if __name__ == '__main__':
    unittest.main()
