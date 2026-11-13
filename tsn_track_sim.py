"""轨迹物理仿真（方案 P1-1 ~ P1-5）。

和"沿路线按固定步长插点"的区别：

* **速度是 OU 过程**，不是白噪声 —— 真人配速有惯性，自相关时间几十秒；
* **GPS 误差是 AR(1) 有色噪声**，不是白噪声 —— 真实误差会缓慢漂移，
  白噪声在频域上一眼就能看出来；
* **步幅亚线性**，并且步频被夹在 155~185 spm —— 这是"步频-配速相关"
  这条检测的核心；
* **主动注入少量跳点** —— 真实轨迹本来就有 0.5% 量级的单点跳变，
  全零反而不是人跑出来的；
* **地理硬约束自检** —— 起点必须在围栏内、必经点必须真的经过、
  相邻采样点之间不允许瞬移（穿墙）、起终点必须贴在底图上。

所有随机性都来自传入的 ``random.Random``，同 seed 可复现。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from loguru import logger

from track_geo import haversine_distance
from track_metrics import stride_for_speed
from track_resample import ResampledTrack, TrackSample, _PathWalker
from track_validate import Issue

METERS_PER_DEG_LAT = 111_320.0
DEFAULT_RHO = 0.92

#: 环境遮挡度 -> 单点 GPS 标准差（米）。开阔操场 3~6m，楼间树下 8~15m。
TERRAIN_SIGMA: Dict[str, Tuple[float, float]] = {
    'open': (3.0, 6.0),
    'campus': (5.0, 12.0),
    'urban': (8.0, 15.0),
}

CADENCE_MIN_SPM = 155.0
CADENCE_MAX_SPM = 185.0

MIN_SEGMENT_FOR_TELEPORT_M = 30.0  # 1 秒内位移超过这个值视为瞬移


@dataclass
class SimOptions:
    """轨迹仿真参数。"""

    terrain: str = 'campus'
    noise_rho: float = DEFAULT_RHO
    sample_interval_s: float = 1.0
    interval_jitter_s: float = 0.08
    ou_theta: float = 0.05
    ou_sigma: float = 0.045
    ramp_s: Tuple[int, int] = (30, 60)
    stop_count: Tuple[int, int] = (2, 4)
    stop_duration_s: Tuple[int, int] = (4, 15)
    stop_factor: Tuple[float, float] = (0.05, 0.35)
    tail_from: float = 0.75
    tail_ratio: Tuple[float, float] = (0.95, 0.97)
    spike_prob: float = 0.006
    spike_distance_m: Tuple[float, float] = (40.0, 80.0)
    step_offset_ratio: float = 0.015
    max_samples: int = 200_000

    def sigma_range(self) -> Tuple[float, float]:
        return TERRAIN_SIGMA.get(self.terrain, TERRAIN_SIGMA['campus'])


def gps_noise(n: int, sigma_m: float | Sequence[float], rho: float = DEFAULT_RHO, rng: Optional[random.Random] = None) -> List[Tuple[float, float]]:
    """AR(1) 有色噪声，返回 ``[(d_north_m, d_east_m), ...]``。

    ``e[i] = rho * e[i-1] + sqrt(1 - rho^2) * N(0, 1) * sigma``，平稳标准差
    恰好等于 ``sigma``。
    """
    if n <= 0:
        return []
    if not 0.0 <= rho < 1.0:
        raise ValueError(f'rho 必须在 [0,1) 内: {rho}')
    rng = rng or random.Random()
    if isinstance(sigma_m, (int, float)):
        sigmas = [float(sigma_m)] * n
    else:
        sigmas = [float(s) for s in sigma_m]
        if len(sigmas) < n:
            sigmas.extend([sigmas[-1] if sigmas else 0.0] * (n - len(sigmas)))
    scale = math.sqrt(1.0 - rho * rho)
    out: List[Tuple[float, float]] = []
    north = east = 0.0
    for i in range(n):
        sigma = sigmas[i]
        north = rho * north + scale * rng.gauss(0.0, 1.0) * sigma
        east = rho * east + scale * rng.gauss(0.0, 1.0) * sigma
        out.append((north, east))
    return out


def terrain_sigmas(n: int, terrain: str = 'campus', rng: Optional[random.Random] = None) -> List[float]:
    """按环境分段给遮挡度：开头/结尾靠近教学楼，中段在操场。"""
    rng = rng or random.Random()
    low, high = TERRAIN_SIGMA.get(terrain, TERRAIN_SIGMA['campus'])
    sigmas: List[float] = []
    for i in range(n):
        ratio = i / max(1, n - 1)
        if ratio < 0.1 or ratio > 0.9:
            sigmas.append(rng.uniform(high * 0.8, high))
        elif ratio < 0.2 or ratio > 0.8:
            sigmas.append(rng.uniform(low * 1.2, high * 0.8))
        else:
            sigmas.append(rng.uniform(low, low * 1.8))
    return sigmas


def apply_gps_noise(points: Sequence[Sequence[float]], sigma_series: Sequence[float], rho: float = DEFAULT_RHO, rng: Optional[random.Random] = None, *, scale_ends: float = 0.15) -> List[List[float]]:
    """把 AR(1) 噪声叠加到经纬度上（米 -> 度，经度按纬度校正）。"""
    rng = rng or random.Random()
    n = len(points)
    if n == 0:
        return []
    noise = gps_noise(n, sigma_series, rho, rng)
    out: List[List[float]] = []
    for i, (lon, lat) in enumerate(points):
        north, east = noise[i]
        if scale_ends < 1.0 and (i == 0 or i == n - 1):
            # 首末点收敛：起点/终点必须和打卡点对得上
            north *= scale_ends
            east *= scale_ends
        d_lat = north / METERS_PER_DEG_LAT
        cos_lat = max(0.05, math.cos(math.radians(lat)))
        d_lon = east / (METERS_PER_DEG_LAT * cos_lat)
        out.append([lon + d_lon, lat + d_lat])
    return out


class PaceOU:
    """配速 OU 过程 + 起步加速 + 中途停顿 + 后程掉速。

    单位统一用 m/s。方案原稿里 ``mu`` 直接给了"每公里秒数"，与 m/s 的
    ``v`` 混用会得到一个恒定偏置的速度，这里按物理量纠正。
    """

    def __init__(self, target_speed_mps: float, rng: random.Random, options: Optional[SimOptions] = None, duration_hint_s: float = 0.0) -> None:
        self.options = options or SimOptions()
        self.rng = rng
        self.target = max(0.3, float(target_speed_mps))
        self.mu = self.target * 1.02  # 补偿停顿和掉速，保证总距离达标
        self.v = self.target
        ramp_lo, ramp_hi = self.options.ramp_s
        self.ramp_s = rng.randint(min(ramp_lo, ramp_hi), max(ramp_lo, ramp_hi))
        self.stops: List[Tuple[int, int]] = []
        self.tail_start = 0
        self.tail_ratio = rng.uniform(*self.options.tail_ratio)
        if duration_hint_s > 0:
            self._plan(duration_hint_s)

    def _plan(self, duration_s: float) -> None:
        n = int(duration_s)
        lo, hi = self.options.stop_count
        for _ in range(self.rng.randint(min(lo, hi), max(lo, hi))):
            if n <= self.ramp_s + 60:
                break
            start = self.rng.randint(self.ramp_s + 30, max(self.ramp_s + 31, n - 20))
            dur = self.rng.randint(*self.options.stop_duration_s)
            self.stops.append((start, dur))
        self.tail_start = int(n * self.options.tail_from)

    def speed_at(self, t: float) -> float:
        """推进一步 OU 并叠加宏观结构。``t`` 是已经过去的秒数。"""
        dt = 1.0
        theta = self.options.ou_theta
        sigma = self.options.ou_sigma
        self.v += theta * (self.mu - self.v) * dt + sigma * math.sqrt(dt) * self.rng.gauss(0.0, 1.0)
        self.v = max(0.15, min(self.mu * 1.6, self.v))
        v = self.v
        if t < self.ramp_s:
            v *= 0.1 + 0.9 * (t / max(1, self.ramp_s))
        for start, dur in self.stops:
            if start <= t < start + dur:
                v *= self.rng.uniform(*self.options.stop_factor)
                break
        if self.tail_start > 0 and t >= self.tail_start:
            span = max(1.0, self.tail_start * 0.34)
            progress = min(1.0, (t - self.tail_start) / span)
            v *= 1.0 - (1.0 - self.tail_ratio) * progress
        return v


def build_speed_curve(duration_s: float, pace_s_per_km: float, rng: Optional[random.Random] = None, options: Optional[SimOptions] = None) -> List[float]:
    """返回逐秒速度序列（m/s），附带宏观结构。"""
    rng = rng or random.Random()
    options = options or SimOptions()
    target = 1000.0 / max(60.0, pace_s_per_km)
    ou = PaceOU(target, rng, options, duration_hint_s=duration_s)
    n = max(1, int(duration_s))
    return [ou.speed_at(float(t)) for t in range(n)]


def cadence_for_speed(speed_mps: float) -> float:
    stride = stride_for_speed(speed_mps)
    if stride <= 0:
        return 0.0
    return speed_mps / stride * 60.0


def inject_spikes(
    points: Sequence[Sequence[float]],
    rng: Optional[random.Random] = None,
    *,
    prob: float = 0.006,
    min_m: float = 40.0,
    max_m: float = 80.0,
) -> Tuple[List[List[float]], List[int]]:
    """单点跳变注入；返回 ``(新点集, 被改动的点下标)``。首末点不动。"""
    rng = rng or random.Random()
    out = [list(p) for p in points]
    hit: List[int] = []
    n = len(out)
    for i in range(1, n - 1):
        if rng.random() >= prob:
            continue
        lon, lat = out[i]
        distance = rng.uniform(min_m, max_m)
        angle = rng.uniform(0.0, 2.0 * math.pi)
        d_lat = distance * math.cos(angle) / METERS_PER_DEG_LAT
        d_lon = distance * math.sin(angle) / (METERS_PER_DEG_LAT * max(0.05, math.cos(math.radians(lat))))
        out[i] = [lon + d_lon, lat + d_lat]
        hit.append(i)
    return out, hit


def offset_total_steps(buckets: Sequence[int], rng: Optional[random.Random] = None, ratio: float = 0.015) -> List[int]:
    """给总步数注入 ±ratio 的漏计/多计偏移，同时保持桶合计不变。"""
    rng = rng or random.Random()
    total = sum(buckets)
    if total <= 0:
        return list(buckets)
    factor = 1.0 + rng.uniform(-ratio, ratio)
    target = max(1, int(round(total * factor)))
    floats = [b * target / total for b in buckets]
    floors = [int(v) for v in floats]
    remainder = target - sum(floors)
    if remainder > 0:
        order = sorted(range(len(floats)), key=lambda i: floats[i] - floors[i], reverse=True)
        for idx in order[:remainder]:
            floors[idx] += 1
    elif remainder < 0:
        order = sorted(range(len(floats)), key=lambda i: floats[i] - floors[i])
        need = -remainder
        for idx in order:
            if need == 0:
                break
            if floors[idx] > 0:
                floors[idx] -= 1
                need -= 1
    return floors


def validate_geo_constraints(
    points: Sequence[Sequence[float]],
    *,
    geofence: Optional[Mapping[str, Any]] = None,
    must_points: Optional[Iterable[Mapping[str, Any]]] = None,
    route: Optional[Sequence[Sequence[float]]] = None,
    ok_radius_m: float = 40.0,
    max_segment_m: float = MIN_SEGMENT_FOR_TELEPORT_M,
    route_tolerance_m: float = 25.0,
) -> List[Issue]:
    """地理硬约束校验：围栏 / 必经点 / 瞬移 / 贴底图。"""
    issues: List[Issue] = []
    if len(points) < 2:
        return [Issue('geo_empty', '轨迹点不足 2 个')]

    fence = _parse_geofence(geofence)
    if fence is not None:
        kind, payload = fence
        start_lon, start_lat = points[0]
        if kind == 'circle':
            center_lat, center_lng, radius = payload
            dist = haversine_distance(start_lat, start_lon, center_lat, center_lng)
            if dist > radius:
                issues.append(Issue('start_outside_fence', f'起点距围栏中心 {dist:.1f}m > 半径 {radius:.0f}m'))
        elif kind == 'polygon':
            if not _point_in_polygon(start_lon, start_lat, payload):
                issues.append(Issue('start_outside_fence', '起点不在围栏多边形内'))

    for idx, must in enumerate(must_points or []):
        try:
            lat = float(must['latitude'] if 'latitude' in must else must.get('a'))
            lon = float(must['longitude'] if 'longitude' in must else must.get('o'))
        except (KeyError, TypeError, ValueError):
            continue
        radius = ok_radius_m
        try:
            radius = float(must.get('okRadius') or must.get('ok_radius') or ok_radius_m)
        except (TypeError, ValueError):
            radius = ok_radius_m
        nearest = min(haversine_distance(lat, lon, p[1], p[0]) for p in points)
        if nearest > radius:
            issues.append(Issue('must_point_missed', f'必经点 #{idx} 最近距离 {nearest:.1f}m > 判定半径 {radius:.0f}m'))

    for i in range(1, len(points)):
        seg = haversine_distance(points[i - 1][1], points[i - 1][0], points[i][1], points[i][0])
        if seg <= max_segment_m:
            continue
        # 单点跳变（spike）：跳出去马上跳回来，前后点距离远小于两段之和。
        # 真实 GPS 本来就有这种点，清洗阶段会剔掉，不该判成穿墙。
        if i + 1 < len(points):
            back = haversine_distance(points[i - 1][1], points[i - 1][0], points[i + 1][1], points[i + 1][0])
            if back < seg * 0.5:
                continue
        if i >= 2:
            prev_back = haversine_distance(points[i - 2][1], points[i - 2][0], points[i][1], points[i][0])
            if prev_back < seg * 0.5:
                continue
        issues.append(Issue('teleport_segment', f'第 {i} 段位移 {seg:.1f}m 超过 {max_segment_m:.0f}m（穿墙/瞬移）', context={'index': i}))

    if route:
        dev_start = min(haversine_distance(points[0][1], points[0][0], p[1], p[0]) for p in route[: max(1, len(route) // 10)])
        if dev_start > route_tolerance_m:
            issues.append(Issue('start_off_route', f'起点偏离底图 {dev_start:.1f}m'))
        dev_end = min(haversine_distance(points[-1][1], points[-1][0], p[1], p[0]) for p in route[-max(1, len(route) // 10) :])
        if dev_end > route_tolerance_m:
            issues.append(Issue('end_off_route', f'终点偏离底图 {dev_end:.1f}m'))
    return issues


def _parse_geofence(geofence: Optional[Mapping[str, Any] | str]) -> Optional[Tuple[str, Any]]:
    if not geofence:
        return None
    data: Any = geofence
    if isinstance(geofence, str):
        import json

        try:
            data = json.loads(geofence)
        except ValueError:
            return None
    if isinstance(data, Mapping):
        lat = data.get('lat') or data.get('latitude') or data.get('centerLat')
        lng = data.get('lng') or data.get('lon') or data.get('longitude') or data.get('centerLng')
        radius = data.get('radius') or data.get('r') or data.get('distance')
        if lat is not None and lng is not None and radius is not None:
            try:
                return ('circle', (float(lat), float(lng), float(radius)))
            except (TypeError, ValueError):
                return None
        for key in ('points', 'polygon', 'coordinates'):
            if isinstance(data.get(key), (list, tuple)):
                return _parse_geofence({'polygon': data[key]})
        return None
    if isinstance(data, (list, tuple)):
        polygon: List[Tuple[float, float]] = []
        for item in data:
            try:
                if isinstance(item, Mapping):
                    polygon.append((float(item.get('longitude', item.get('lng'))), float(item.get('latitude', item.get('lat')))))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    polygon.append((float(item[0]), float(item[1])))
            except (TypeError, ValueError):
                continue
        if len(polygon) >= 3:
            return ('polygon', polygon)
    return None


def _point_in_polygon(lon: float, lat: float, polygon: Sequence[Tuple[float, float]]) -> bool:
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if ((y1 > lat) != (y2 > lat)) and (lon < (x2 - x1) * (lat - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
    return inside


def simulate_run(
    path_points: Sequence[Sequence[float]],
    need_distance_m: float,
    start_timestamp_ms: int,
    pace_s_per_km: float,
    *,
    rng: Optional[random.Random] = None,
    options: Optional[SimOptions] = None,
) -> ResampledTrack:
    """按 OU 速度 + AR(1) 噪声生成一条"人跑的"轨迹。

    返回的 :class:`ResampledTrack` 与原有重采样完全兼容（``samples`` /
    ``points`` / ``step_samples`` 都在），因此下游 ``to_legacy_points``
    与 ``validate_track`` 不需要改。
    """
    rng = rng or random.Random()
    options = options or SimOptions()
    if need_distance_m <= 0:
        raise ValueError('目标距离必须为正')
    points = [[float(p[0]), float(p[1])] for p in path_points]
    if len(points) < 2:
        raise ValueError('路线至少需要两个点')

    target_speed = 1000.0 / max(60.0, pace_s_per_km)
    ou = PaceOU(target_speed, rng, options)
    walker = _PathWalker(points, 'pingpong')
    samples: List[TrackSample] = []
    traveled = 0.0
    offset_s = 0.0
    while traveled < need_distance_m - 1e-9:
        if len(samples) >= options.max_samples:
            raise ValueError(f'采样点超过上限 {options.max_samples}')
        speed = ou.speed_at(offset_s)
        dt_s = max(0.2, options.sample_interval_s + rng.uniform(-options.interval_jitter_s, options.interval_jitter_s))
        distance = speed * dt_s
        remaining = need_distance_m - traveled
        if distance >= remaining:
            distance = remaining
            dt_s = max(0.15, distance / speed) if speed > 0 else 0.15
        walker.advance(distance)
        offset_s += dt_s
        traveled += distance
        lon, lat = walker.current_point()
        samples.append(
            TrackSample(
                lon=lon,
                lat=lat,
                t_ms=float(start_timestamp_ms) + offset_s * 1000.0,
                dt_ms=dt_s * 1000.0,
                offset_s=offset_s,
                distance_m=distance,
                cumulative_m=traveled,
                speed_mps=speed,
            )
        )

    raw = [[s.lon, s.lat] for s in samples]
    sigmas = terrain_sigmas(len(raw), options.terrain, rng)
    noisy = apply_gps_noise(raw, sigmas, options.noise_rho, rng)
    noisy, hits = inject_spikes(noisy, rng, prob=options.spike_prob, min_m=options.spike_distance_m[0], max_m=options.spike_distance_m[1])
    for i, (lon, lat) in enumerate(noisy):
        samples[i].lon = lon
        samples[i].lat = lat
    if options.spike_prob > 0 and not hits:
        # 一次跳点都没有反而异常：极少数情况下补一个
        if len(samples) > 40 and rng.random() < 0.5:
            idx = rng.randint(5, len(samples) - 6)
            distance = rng.uniform(*options.spike_distance_m)
            angle = rng.uniform(0.0, 2.0 * math.pi)
            lat0 = samples[idx].lat
            samples[idx].lon += distance * math.sin(angle) / (METERS_PER_DEG_LAT * max(0.05, math.cos(math.radians(lat0))))
            samples[idx].lat += distance * math.cos(angle) / METERS_PER_DEG_LAT
            hits = [idx]

    samples[-1].paused = True
    samples[-2].paused = True
    track = ResampledTrack(
        samples=samples,
        target_distance_m=float(need_distance_m),
        base_speed_mps=target_speed,
        plan_use_time_s=float(need_distance_m) / target_speed,
        start_timestamp_ms=float(start_timestamp_ms),
        source_point_count=len(points),
        cleaned_point_count=len(points),
        notes=[f'terrain={options.terrain}', f'spikes={len(hits)}'],
    )
    logger.info(
        f'轨迹仿真完成：{track.total_distance_m:.0f}m / {track.elapsed_s:.0f}s，'
        f'{len(samples)} 点，配速 {pace_s_per_km:.0f}s/km，跳点 {len(hits)} 个'
    )
    return track


def describe_simulation(track: ResampledTrack) -> Dict[str, Any]:
    # 步频只在"真在跑"的段落上有意义（停步/慢走段速度趋近 0）
    speeds = [s.speed_mps for s in track.samples if s.speed_mps >= 1.0]
    if not speeds:
        speeds = [s.speed_mps for s in track.samples if s.speed_mps > 0]
    strides = [stride_for_speed(v) for v in speeds]
    cadences = [v / st * 60.0 for v, st in zip(speeds, strides, strict=False) if st > 0]
    return {
        'distance_m': round(track.total_distance_m, 1),
        'elapsed_s': round(track.elapsed_s, 1),
        'points': len(track.samples),
        'avg_speed_mps': round(sum(speeds) / len(speeds), 3) if speeds else 0.0,
        'speed_std': round(_std(speeds), 3),
        'cadence_min': round(min(cadences), 1) if cadences else 0.0,
        'cadence_max': round(max(cadences), 1) if cadences else 0.0,
        'step_total': track.total_steps,
        'notes': track.notes,
    }


def _std(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
