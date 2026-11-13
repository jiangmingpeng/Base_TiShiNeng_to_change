import random

import pytest

from TiShiNengRunPathManage import (
    align_step_fields,
    genLegacyRunPathRepeat,
    genTiShiNengRunPathRepeat,
    to_legacy_points,
)
from tsn_track_sim import SimOptions


ROUTE = [[113.30 + i * 0.0002, 23.10 + i * 0.0001] for i in range(200)]
START = 1_763_000_000_000


def test_distance_matches_target():
    _, _, distance = genTiShiNengRunPathRepeat(ROUTE, 2500.0, START, 1200.0, True, rng=random.Random(1))
    assert abs(distance - 2500.0) < 1.0


def test_bucket_sum_equals_last_cumulative_steps():
    geo, buckets, _ = genTiShiNengRunPathRepeat(ROUTE, 2500.0, START, 1200.0, True, rng=random.Random(2))
    assert sum(buckets) == geo[-1]['e']


def test_steps_are_monotonic():
    geo, _, _ = genTiShiNengRunPathRepeat(ROUTE, 1500.0, START, 700.0, True, rng=random.Random(3))
    steps = [p['e'] for p in geo]
    assert all(steps[i] <= steps[i + 1] for i in range(len(steps) - 1))


def test_timestamps_strictly_increase():
    geo, _, _ = genTiShiNengRunPathRepeat(ROUTE, 1200.0, START, 600.0, True, rng=random.Random(4))
    stamps = [p['t'] for p in geo]
    assert all(stamps[i] > stamps[i - 1] for i in range(1, len(stamps)))


def test_private_layout_fields():
    geo, _, _ = genTiShiNengRunPathRepeat(ROUTE, 1000.0, START, 500.0, False, rng=random.Random(5))
    assert {'latitude', 'longitude', 'time', 'speed', 'puase', 'stability'} <= set(geo[0])
    assert 'a' not in geo[0]


def test_public_layout_fields():
    geo, _, _ = genTiShiNengRunPathRepeat(ROUTE, 1000.0, START, 500.0, True, rng=random.Random(6))
    assert {'a', 'o', 't', 'e', 'c', 'i', 'l', 's'} <= set(geo[0])


def test_step_offset_stays_within_band():
    totals = []
    for seed in range(6):
        _geo, buckets, _ = genTiShiNengRunPathRepeat(ROUTE, 2000.0, START, 1000.0, True, rng=random.Random(seed))
        totals.append(sum(buckets))
    assert all(2000 < total for total in totals)
    assert len(set(totals)) > 1, '每个 seed 的步数应当有差异（漏计/多计特征）'


def test_legacy_generator_still_works_as_baseline():
    geo, buckets, distance = genLegacyRunPathRepeat(ROUTE, 1000.0, START, 500.0, True)
    assert abs(distance - 1000.0) < 1.0
    assert sum(buckets) > 0


def test_align_step_fields_forces_last_value():
    points = [{'e': 0}, {'e': 10}, {'e': 21}]
    aligned = align_step_fields(points, 100)
    assert aligned[-1]['e'] == 100
    assert all(aligned[i]['e'] <= aligned[i + 1]['e'] for i in range(len(aligned) - 1))


def test_invalid_target_distance_raises():
    with pytest.raises(ValueError):
        genTiShiNengRunPathRepeat(ROUTE, 0.0, START, 500.0, True)


def test_single_point_route_raises():
    with pytest.raises(ValueError):
        genTiShiNengRunPathRepeat([[113.3, 23.1]], 1000.0, START, 500.0, True)


def test_to_legacy_points_marks_last_two_paused():
    from tsn_track_sim import simulate_run

    track = simulate_run(ROUTE, 500.0, START, 300.0, rng=random.Random(7), options=SimOptions())
    legacy = to_legacy_points(track, isPublic=True)
    assert legacy[-1]['i'] is True and legacy[-2]['i'] is True
