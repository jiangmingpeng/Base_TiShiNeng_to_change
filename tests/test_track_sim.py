import math

import tsn_track_sim as sim


ROUTE = [[113.30 + i * 0.0002, 23.10 + i * 0.0001] for i in range(200)]


def test_gps_noise_stationary_sigma():
    import random

    sigma = 8.0
    noise = sim.gps_noise(4000, sigma, rho=0.92, rng=random.Random(1))
    values = [n for pair in noise for n in pair]
    mean = sum(values) / len(values)
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    assert 5.6 < std < 11.0, std


def test_gps_noise_is_autocorrelated():
    """白噪声的 lag-1 相关系数约为 0；AR(1) rho=0.92 应当明显为正。"""
    import random

    north = [p[0] for p in sim.gps_noise(3000, 1.0, rho=0.92, rng=random.Random(2))]
    mean = sum(north) / len(north)
    var = sum((v - mean) ** 2 for v in north)
    cov = sum((north[i] - mean) * (north[i - 1] - mean) for i in range(1, len(north)))
    corr = cov / var
    assert corr > 0.6, corr


def test_apply_gps_noise_keeps_endpoints_tight():
    import random

    points = [[113.30, 23.10], [113.31, 23.11], [113.32, 23.12]]
    noisy = sim.apply_gps_noise(points, [10.0, 10.0, 10.0], rng=random.Random(3))
    assert abs(noisy[0][0] - points[0][0]) < 0.00002
    assert abs(noisy[2][1] - points[2][1]) < 0.00002


def test_stride_and_cadence_stay_physiological():
    from track_metrics import CADENCE_MAX_SPM, CADENCE_MIN_SPM, stride_for_speed

    for speed in (1.6, 2.0, 2.5, 2.8, 3.2, 3.6):
        stride = stride_for_speed(speed)
        cadence = speed / stride * 60.0
        assert CADENCE_MIN_SPM - 0.5 <= cadence <= CADENCE_MAX_SPM + 0.5, (speed, stride, cadence)


def test_stride_is_sublinear():
    from track_metrics import stride_for_speed

    s1 = stride_for_speed(2.0)
    s2 = stride_for_speed(4.0)
    assert s2 < s1 * 2, '步幅应随速度次线性增长'


def test_pace_curve_has_ramp_and_tail():
    import random

    curve = sim.build_speed_curve(900.0, 300.0, rng=random.Random(4))
    assert len(curve) == 900
    assert curve[0] < curve[60], '起步应当加速'
    assert sum(curve) / len(curve) < 1000 / 300 * 1.2


def test_inject_spikes_moves_points_40_to_80m():
    import random

    points = [[113.30 + i * 0.0001, 23.10] for i in range(300)]
    noisy, hits = sim.inject_spikes(points, random.Random(5), prob=1.0, min_m=40, max_m=80)
    assert len(noisy) == len(points)
    assert hits, 'prob=1 时必然有跳点'
    from track_geo import haversine_distance

    for idx in hits:
        moved = haversine_distance(points[idx][1], points[idx][0], noisy[idx][1], noisy[idx][0])
        assert 35 <= moved <= 85, moved
    assert noisy[0] == points[0] and noisy[-1] == points[-1]


def test_offset_total_steps_keeps_sum_and_ratio():
    import random

    buckets = [100, 120, 90, 110]
    out = sim.offset_total_steps(buckets, random.Random(6), ratio=0.015)
    assert abs(sum(out) - sum(buckets)) <= sum(buckets) * 0.02
    assert all(v >= 0 for v in out)


def test_geofence_violation_detected():
    points = [[113.30, 23.10], [113.301, 23.1005], [113.302, 23.101]]
    issues = sim.validate_geo_constraints(points, geofence={'lat': 23.20, 'lng': 113.40, 'radius': 100})
    assert any(i.code == 'start_outside_fence' for i in issues)


def test_must_point_missed_detected():
    points = [[113.30, 23.10], [113.301, 23.1005]]
    must = [{'latitude': 23.20, 'longitude': 113.40, 'okRadius': 40}]
    issues = sim.validate_geo_constraints(points, must_points=must)
    assert any(i.code == 'must_point_missed' for i in issues)


def test_single_spike_is_not_a_teleport():
    # 正常点间距约 1 秒 3 米，中间一个点被 GPS 甩出去 60 米又回来
    points = [[113.30 + i * 0.00003, 23.10] for i in range(12)]
    spikes = [list(p) for p in points]
    spikes[6] = [points[6][0], points[6][1] + 60.0 / 111320.0]
    issues = sim.validate_geo_constraints(spikes)
    assert not any(i.code == 'teleport_segment' for i in issues)


def test_teleport_detected():
    points = [[113.30, 23.10], [113.301, 23.1005], [113.40, 23.20], [113.401, 23.2005]]
    issues = sim.validate_geo_constraints(points)
    assert any(i.code == 'teleport_segment' for i in issues)


def test_simulate_run_distance_and_time_are_coherent():
    import random

    track = sim.simulate_run(ROUTE, 2500.0, 1_763_000_000_000, 300.0, rng=random.Random(7))
    assert abs(track.total_distance_m - 2500.0) < 0.5
    stats = sim.describe_simulation(track)
    assert 600 < stats['elapsed_s'] < 1200, stats
    assert 155 <= stats['cadence_max'] <= 185
    stamps = track.timestamps_ms
    assert all(stamps[i] > stamps[i - 1] for i in range(1, len(stamps)))


def test_simulate_run_is_reproducible():
    import random

    a = sim.simulate_run(ROUTE, 1000.0, 0, 300.0, rng=random.Random(11))
    b = sim.simulate_run(ROUTE, 1000.0, 0, 300.0, rng=random.Random(11))
    assert a.points[:20] == b.points[:20]
