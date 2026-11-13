from __future__ import annotations
import random
from typing import List, Optional, Sequence, Tuple
from track_log import logger
from step_buckets import DEFAULT_BUCKET_SECONDS, BucketAllocation, StepInterval, allocate_steps
from track_clean import InsufficientTrackError
from track_geo import EARTH_RADIUS_KM, EARTH_RADIUS_M, METERS_PER_KM, LocalProjection, haversine_distance, normalize_points, point_list_distance
from track_metrics import cadence_spm, compute_metrics, format_pace, pace_seconds_per_km, stride_for_speed, steps_for_interval
from track_resample import ResampleOptions, ResampledTrack, TrackGenerationError, resample_track
from track_validate import ValidationReport, validate_track
from tsn_track_sim import SimOptions, describe_simulation, offset_total_steps, simulate_run, validate_geo_constraints
__all__ = ['EARTH_RADIUS_KM', 'EARTH_RADIUS_M', 'METERS_PER_KM', 'TsnRunPolyline', 'genTiShiNengRunPathRepeat', 'generate_run_track', 'to_legacy_points', 'haversine_distance', 'getPointListDistance', 'LocalProjection', 'ResampleOptions', 'ResampledTrack', 'TrackGenerationError', 'InsufficientTrackError']

def getPointListDistance(pointList: Sequence[Sequence[float]]) -> float:
    return point_list_distance(pointList)

class TsnRunPolyline:

    def __init__(self, points: Sequence[Sequence[float]]):
        self.points = normalize_points(points)
        self.distances = [haversine_distance(self.points[i][1], self.points[i][0], self.points[i + 1][1], self.points[i + 1][0]) for i in range(len(self.points) - 1)]
        self.total_length = sum(self.distances)

    def haversine_distance(self, point1: Sequence[float], point2: Sequence[float]) -> float:
        return haversine_distance(point1[1], point1[0], point2[1], point2[0])

    def interpolate_point(self, point1: Sequence[float], point2: Sequence[float], ratio: float) -> Tuple[float, float]:
        projection = LocalProjection(point1[0], point1[1])
        return projection.interpolate(point1[0], point1[1], point2[0], point2[1], ratio)

    def simulate_motion(self, avg_speed: float, distance: float):
        if avg_speed <= 0:
            raise TrackGenerationError(f'平均速度必须为正: {avg_speed}')
        plan_use_time = distance / avg_speed
        track = resample_track(points=self.points, need_distance_m=distance, start_timestamp_ms=0.0, plan_use_time_s=plan_use_time, options=ResampleOptions(speed_jitter=0.08))
        sampled = [{'lat': s.lat, 'lon': s.lon, 'millisecond': s.dt_ms, 'speed': s.speed_mps, 'distance': s.distance_m} for s in track.samples]
        logger.info(f'traveled_distance:{track.total_distance_m}')
        return (sampled, track.total_distance_m)

def to_legacy_points(track: ResampledTrack, isPublic: bool=True, coord_decimals: int=7, speed_decimals: int=6) -> List[dict]:
    result: List[dict] = []
    cumulative_steps = 0.0
    for sample in track.samples:
        cumulative_steps += steps_for_interval(sample.distance_m, sample.speed_mps)
        lat = round(sample.lat, coord_decimals)
        lon = round(sample.lon, coord_decimals)
        speed = round(sample.speed_mps, speed_decimals)
        steps_int = int(round(cumulative_steps))
        if isPublic:
            result.append({'a': lat, 'c': int(sample.offset_s), 'e': steps_int, 'i': bool(sample.paused), 'l': 1, 'o': lon, 's': speed, 't': int(sample.t_ms), 'distance': sample.distance_m})
        else:
            result.append({'countTime': 0, 'latitude': lat, 'locationType': 1, 'longitude': lon, 'puase': bool(sample.paused), 'speed': speed, 'stability': 0, 'time': int(sample.t_ms), 'distance': sample.distance_m})
    if len(result) >= 2 and isPublic:
        result[-1]['c'] = result[-2]['c']
        result[-1]['e'] = result[-2]['e']
        result[-1]['i'] = True
        result[-2]['i'] = True
    return result

def generate_run_track(pointList: Sequence[Sequence[float]], needDistance: float, startTimeStamp: float, planUseTime: float, isPublic: bool=True, options: Optional[ResampleOptions]=None, rng: Optional[random.Random]=None, risk_resolver=None) -> Tuple[ResampledTrack, BucketAllocation, ValidationReport]:
    track = resample_track(points=pointList, need_distance_m=needDistance, start_timestamp_ms=startTimeStamp, plan_use_time_s=planUseTime, options=options, rng=rng, risk_resolver=risk_resolver)
    intervals = [StepInterval(start_offset_s=start, duration_s=duration, steps=steps) for start, duration, steps in track.step_samples()]
    allocation = allocate_steps(intervals, bucket_seconds=DEFAULT_BUCKET_SECONDS, estimated=False, total_steps=int(round(sum((iv.steps for iv in intervals)))))
    report = validate_track(track, bucket_allocation=allocation, total_steps=allocation.total_steps, summary_coords=[track.points[0], track.points[-1]])
    if not report.ok:
        logger.warning(report.summary())
    return (track, allocation, report)


def align_step_fields(result: Sequence[dict], target_total: int) -> List[dict]:
    """把 legacy 点里的累计步数 ``e`` 对齐到分桶合计。

    服务端会拿步数桶求和与轨迹里的累计步数互相校验，两者差 2 步就是
    "数据不合格"。这里按比例缩放并强制单调，末点精确等于 ``target_total``。
    """
    if not result:
        return list(result)
    last = float(result[-1].get('e') or 0)
    factor = (target_total / last) if last > 0 else 1.0
    prev = 0
    for item in result:
        value = int(round(float(item.get('e') or 0) * factor))
        value = max(prev, min(target_total, value))
        item['e'] = value
        prev = value
    result[-1]['e'] = target_total
    if len(result) >= 2:
        result[-2]['e'] = min(result[-2]['e'], target_total)
    return list(result)

def genTiShiNengRunPathRepeat(pointList: Sequence[Sequence[float]], needDistance: float, startTimeStamp: float, planUseTime: float, isPublic: bool=True, *, rng: Optional[random.Random]=None, sim: Optional[SimOptions]=None, pace_s_per_km: Optional[float]=None) -> Tuple[List[dict], List[int], float]:
    """轨迹生成主入口：OU 配速 + AR(1) GPS 噪声 + 跳点 + 步数偏移。

    返回 ``(legacy 点列, 60s 步数桶, 实际总距离)``，与旧实现签名一致，
    下游 `tsnRunServer` 不需要感知底层换成了物理仿真。
    """
    rng = rng or random.Random()
    sim = sim or SimOptions()
    pace = pace_s_per_km or planUseTime / max(1e-6, needDistance / 1000.0)
    track = simulate_run(pointList, needDistance, int(startTimeStamp), pace, rng=rng, options=sim)
    intervals = [StepInterval(start_offset_s=start, duration_s=duration, steps=steps) for start, duration, steps in track.step_samples()]
    allocation = allocate_steps(intervals, bucket_seconds=DEFAULT_BUCKET_SECONDS, estimated=False, total_steps=int(round(sum((iv.steps for iv in intervals)))))
    # 真实记录会漏计/多计 1~2%，但桶内合计必须自洽
    buckets = offset_total_steps(allocation.buckets, rng, sim.step_offset_ratio)
    adjusted = BucketAllocation(buckets=buckets, total_steps=sum(buckets), bucket_seconds=allocation.bucket_seconds)
    report = validate_track(track, bucket_allocation=adjusted, total_steps=adjusted.total_steps, summary_coords=[track.points[0], track.points[-1]])
    if not report.ok:
        report.raise_if_failed()
    geo_issues = validate_geo_constraints(track.points)
    if geo_issues:
        logger.warning('地理约束告警: ' + '; '.join(f'{i.code}: {i.message}' for i in geo_issues))
    result = align_step_fields(to_legacy_points(track, isPublic=isPublic), adjusted.total_steps)
    logger.info(f'needDistance:{needDistance},sumDistance:{track.total_distance_m} stepBuckets:{len(buckets)}/{adjusted.total_steps} {describe_simulation(track)}')
    return (result, list(buckets), track.total_distance_m)


def genLegacyRunPathRepeat(pointList: Sequence[Sequence[float]], needDistance: float, startTimeStamp: float, planUseTime: float, isPublic: bool=True) -> Tuple[List[dict], List[int], float]:
    """旧的重采样实现（白噪声速度、无 GPS 噪声），保留用于 A/B 对拍。"""
    track, allocation, report = generate_run_track(pointList=pointList, needDistance=needDistance, startTimeStamp=startTimeStamp, planUseTime=planUseTime, isPublic=isPublic)
    if not report.ok:
        report.raise_if_failed()
    result = to_legacy_points(track, isPublic=isPublic)
    logger.info(f'needDistance:{needDistance},sumDistance:{track.total_distance_m} stepBuckets:{len(allocation.buckets)}/{allocation.total_steps}')
    return (result, list(allocation.buckets), track.total_distance_m)

def describe_track(track: ResampledTrack) -> dict:
    chord = compute_metrics(track.points, track.timestamps_ms)
    elapsed = track.elapsed_s
    avg_speed = track.total_distance_m / elapsed if elapsed > 0 else 0.0
    return {'distance_m': round(track.total_distance_m, 3), 'chord_distance_m': round(chord.distance_m, 3), 'elapsed_s': round(elapsed, 1), 'point_count': len(track.samples), 'avg_speed_mps': round(avg_speed, 4), 'pace': format_pace(pace_seconds_per_km(track.total_distance_m, elapsed)), 'cadence_spm': round(cadence_spm(avg_speed), 1), 'stride_m': round(stride_for_speed(avg_speed), 3), 'steps': track.total_steps, 'cleaned_points': track.cleaned_point_count, 'source_points': track.source_point_count, 'notes': track.notes}
