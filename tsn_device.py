"""设备指纹：账号级确定性绑定 + 物理自洽校验（对应方案 P0-3）。

核心不变量：一个账号永远对应同一台"物理手机"。
``android_id`` 由 ``account_id + model`` 哈希得到，因此同一账号跨进程、
跨机器重跑都得到同一指纹；不同账号不会撞到同一台设备。

机型池按省份做加权采样，避免出现"全校都是同一台小米旗舰"这种
D2/D4 聚类特征。真实校园用户分布里中端 Redmi / vivo Y 系列 /
OPPO A 系列 / HONOR X 系列占比最高，旗舰是长尾。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

TAG = 'tsn_device'

#: APK 最低支持 Android 6.0（API 23）。低于此版本的 os 一律不自洽。
APK_MIN_SDK = 23
APK_VERSION_NAME = '2.0.23'
APK_VERSION_CODE = 20230

#: 单个账号允许的 uptime 区间（秒）：30 分钟 ~ 30 天。
UPTIME_MIN_S = 30 * 60
UPTIME_MAX_S = 30 * 24 * 3600

_BUILD_PREFIX_BY_API = {26: 'OPM1', 28: 'PPR1', 29: 'QQ3A', 30: 'RQ3A', 31: 'SP1A', 33: 'TQ3A', 34: 'UP1A', 35: 'AP3A', 36: 'BP2A'}

_ANDROID_API_BY_MAJOR = {
    5: 22,
    6: 23,
    7: 24,
    8: 26,
    9: 28,
    10: 29,
    11: 30,
    12: 31,
    13: 33,
    14: 34,
    15: 35,
    16: 36,
}


@dataclass(frozen=True)
class DeviceProfile:
    """一台真实机型的三元组与显示参数。"""

    manufacturer: str
    model: str
    board: str
    device: str
    screen_w: int
    screen_h: int
    density: int
    abis: Tuple[str, ...]
    os_versions: Tuple[str, ...]
    tier: str = 'mid'
    weight: float = 1.0

    @property
    def dp_width(self) -> float:
        return self.screen_w * 160.0 / self.density

    @property
    def dp_height(self) -> float:
        return self.screen_h * 160.0 / self.density

    @property
    def abi(self) -> str:
        return self.abis[0]


def _p(manufacturer: str, model: str, board: str, device: str, w: int, h: int, dpi: int, os_versions: Sequence[str], tier: str = 'mid', weight: float = 1.0) -> DeviceProfile:
    return DeviceProfile(
        manufacturer=manufacturer,
        model=model,
        board=board,
        device=device,
        screen_w=w,
        screen_h=h,
        density=dpi,
        abis=('arm64-v8a', 'armeabi-v7a', 'armeabi'),
        os_versions=tuple(os_versions),
        tier=tier,
        weight=weight,
    )


#: 机型目录：``model`` -> 三元组。model 必须是真实可查的公开型号名，
#: board/device 用厂商代号；同一 model 不允许对应多个 board。
DEVICE_CATALOG: Dict[str, DeviceProfile] = {
    p.model: p
    for p in (
        # 小米 / Redmi（校园主力，中端权重高）
        _p('Xiaomi', '23127PN0CC', 'vermeer', 'vermeer', 1080, 2400, 480, ('13', '14', '15'), 'mid', 1.6),
        _p('Xiaomi', '2312DRA50C', 'gold', 'gold', 1080, 2400, 480, ('13', '14'), 'entry', 1.9),
        _p('Xiaomi', '23049RAD8C', 'marble', 'marble', 1080, 2400, 480, ('13', '14', '15'), 'mid', 1.4),
        _p('Xiaomi', '25042PN24C', 'sun', 'sun', 1220, 2712, 480, ('15', '16'), 'mid', 1.2),
        _p('Xiaomi', '24069PC21C', 'duchamp', 'duchamp', 1220, 2712, 480, ('14', '15'), 'mid', 1.0),
        _p('Redmi', '22101316C', 'rubens', 'rubens', 1080, 2400, 480, ('13', '14'), 'entry', 1.7),
        # OPPO / realme
        _p('OPPO', 'PJZ110', 'tundra', 'tundra', 1264, 2780, 560, ('14', '15'), 'mid', 1.3),
        _p('OPPO', 'PKD130', 'aston', 'aston', 1080, 2412, 480, ('14', '15'), 'mid', 1.5),
        _p('OPPO', 'PHY110', 'salami', 'salami', 1264, 2780, 560, ('14', '15'), 'high', 0.7),
        _p('realme', 'RMX3820', 'RE58B2', 'RE58B2', 1080, 2400, 480, ('13', '14'), 'mid', 1.1),
        # vivo / iQOO
        _p('vivo', 'V2318A', 'PD2318', 'PD2318', 1080, 2400, 480, ('13', '14'), 'mid', 1.6),
        _p('vivo', 'V2309A', 'PD2309', 'PD2309', 1260, 2800, 560, ('14', '15'), 'high', 0.8),
        _p('iQOO', 'V2307A', 'PD2307', 'PD2307', 1260, 2800, 560, ('14', '15'), 'mid', 0.9),
        # HONOR / HUAWEI
        _p('HONOR', 'ALI-AN00', 'ALI', 'ALI', 1080, 2412, 480, ('14',), 'mid', 1.2),
        _p('HONOR', 'PGT-AN00', 'PGT', 'PGT', 1224, 2688, 480, ('13', '14'), 'high', 0.6),
        _p('HUAWEI', 'ALN-AL00', 'ALN', 'ALN', 1260, 2720, 480, ('12', '13'), 'high', 0.5),
        _p('HUAWEI', 'BON-AL00', 'BON', 'BON', 1224, 2700, 480, ('12', '13'), 'mid', 0.6),
        # 三星 / 一加
        _p('samsung', 'SM-S9180', 'dm3q', 'dm3q', 1440, 3088, 560, ('14', '15'), 'high', 0.35),
        _p('samsung', 'SM-A5460', 'a54x', 'a54x', 1080, 2340, 440, ('13', '14'), 'mid', 0.5),
        _p('OnePlus', 'PJD110', 'salad', 'salad', 1240, 2772, 450, ('14', '15'), 'high', 0.45),
    )
}

#: 省份 -> 品牌偏好（只在目录内做加权，不会凭空发明机型）。
PROVINCE_BRAND_BIAS: Dict[str, Dict[str, float]] = {
    '广东': {'OPPO': 1.45, 'vivo': 1.35, 'realme': 1.3, 'Xiaomi': 1.0},
    '江苏': {'Xiaomi': 1.3, 'HONOR': 1.2, 'HUAWEI': 1.2},
    '浙江': {'Xiaomi': 1.25, 'vivo': 1.15, 'OPPO': 1.1},
    '山东': {'HONOR': 1.3, 'Xiaomi': 1.2, 'vivo': 1.1},
    '河南': {'Xiaomi': 1.35, 'OPPO': 1.2, 'vivo': 1.2},
    '四川': {'Xiaomi': 1.2, 'vivo': 1.25, 'OPPO': 1.15},
    '湖北': {'Xiaomi': 1.2, 'HONOR': 1.15, 'OPPO': 1.1},
    '湖南': {'vivo': 1.3, 'OPPO': 1.25, 'Xiaomi': 1.1},
    '河北': {'HONOR': 1.25, 'Xiaomi': 1.2},
    '安徽': {'Xiaomi': 1.3, 'vivo': 1.15},
    '福建': {'OPPO': 1.3, 'vivo': 1.2},
    '陕西': {'Xiaomi': 1.25, 'HONOR': 1.15},
    '辽宁': {'HUAWEI': 1.2, 'Xiaomi': 1.15},
    '江西': {'vivo': 1.25, 'OPPO': 1.2},
    '广西': {'OPPO': 1.25, 'vivo': 1.2},
    '云南': {'vivo': 1.3, 'OPPO': 1.2},
    '山西': {'HONOR': 1.2, 'Xiaomi': 1.2},
    '重庆': {'OPPO': 1.2, 'vivo': 1.2, 'Xiaomi': 1.1},
    '北京': {'Xiaomi': 1.3, 'HUAWEI': 1.1, 'OnePlus': 1.1},
    '上海': {'Xiaomi': 1.2, 'OPPO': 1.15, 'samsung': 1.2},
    '天津': {'Xiaomi': 1.2, 'HONOR': 1.15},
    '内蒙古': {'HONOR': 1.2, 'Xiaomi': 1.2},
    '黑龙江': {'HUAWEI': 1.2, 'Xiaomi': 1.15},
    '吉林': {'Xiaomi': 1.2, 'HONOR': 1.1},
    '贵州': {'vivo': 1.25, 'OPPO': 1.2},
    '甘肃': {'HONOR': 1.2, 'vivo': 1.15},
    '青海': {'Xiaomi': 1.2, 'HONOR': 1.1},
    '宁夏': {'Xiaomi': 1.2, 'vivo': 1.1},
    '新疆': {'vivo': 1.2, 'HONOR': 1.15},
    '西藏': {'HONOR': 1.2, 'Xiaomi': 1.1},
    '海南': {'OPPO': 1.2, 'vivo': 1.2},
}

DEVICE_POOL_OVERRIDE = os.getenv('TSN_DEVICE_POOL', '')


def load_device_pool(path: Optional[str] = None) -> Dict[str, DeviceProfile]:
    """加载机型池；``TSN_DEVICE_POOL`` 指向 JSON 时与内置目录合并。"""
    pool = dict(DEVICE_CATALOG)
    target = path or DEVICE_POOL_OVERRIDE
    if not target:
        return pool
    try:
        with open(target, 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return pool
    for item in raw.get('devices', []) if isinstance(raw, dict) else []:
        try:
            profile = DeviceProfile(
                manufacturer=str(item['manufacturer']),
                model=str(item['model']),
                board=str(item['board']),
                device=str(item.get('device') or item['board']),
                screen_w=int(item['screen_w']),
                screen_h=int(item['screen_h']),
                density=int(item['density']),
                abis=tuple(item.get('abis') or ('arm64-v8a',)),
                os_versions=tuple(str(v) for v in item.get('os_versions') or ('14',)),
                tier=str(item.get('tier') or 'mid'),
                weight=float(item.get('weight') or 1.0),
            )
        except (KeyError, TypeError, ValueError):
            continue
        pool[profile.model] = profile
    return pool


def _normalize_province(province: Optional[str]) -> str:
    text = (province or '').strip()
    for suffix in ('省', '市', '自治区', '壮族', '回族', '维吾尔'):
        text = text.replace(suffix, '')
    return text


def _weighted_profiles(province: Optional[str], pool: Mapping[str, DeviceProfile]) -> Tuple[List[DeviceProfile], List[float]]:
    bias = PROVINCE_BRAND_BIAS.get(_normalize_province(province), {})
    profiles: List[DeviceProfile] = []
    weights: List[float] = []
    for profile in pool.values():
        weight = profile.weight * bias.get(profile.manufacturer, 1.0)
        if weight <= 0:
            continue
        profiles.append(profile)
        weights.append(weight)
    if not profiles:
        profiles = list(pool.values())
        weights = [p.weight for p in profiles]
    return profiles, weights


@dataclass
class TsnDeviceRecord:
    """对应 ``tsn_device`` 表一行。"""

    device_pk: str
    account_id: str
    android_id: str
    manufacturer: str
    model: str
    board: str
    os_version: str
    abi: str
    screen_w: int
    screen_h: int
    density: int
    boot_time_ms: int
    apk_version: str = APK_VERSION_NAME
    bound_at: int = 0
    last_seen_ip: Optional[str] = None
    last_seen_lat: Optional[float] = None
    last_seen_lng: Optional[float] = None
    province: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def android_api(self) -> int:
        try:
            return _ANDROID_API_BY_MAJOR.get(int(str(self.os_version).split('.')[0]), 0)
        except (TypeError, ValueError):
            return 0

    def dp_metrics(self) -> Tuple[float, float]:
        return (self.screen_w * 160.0 / self.density, self.screen_h * 160.0 / self.density)

    def build_id(self) -> str:
        """Android build id：按 API 版本给真实前缀，尾号由 android_id 派生（确定性）。"""
        prefix = _BUILD_PREFIX_BY_API.get(self.android_api, 'UP1A')
        seq = int(self.android_id[:4] or '0', 16) % 900 + 100
        return f'{prefix}.{self.android_api}1005.{seq:03d}'

    def webview_user_agent(self) -> str:
        """WebView UA：``Android x; MODEL Build/...`` 必须与 model/os 一致。"""
        return f'Mozilla/5.0 (Linux; Android {self.os_version}; {self.model} Build/{self.build_id()}; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/120.0.0.0 Mobile Safari/537.36'

    def okhttp_user_agent(self) -> str:
        from tsn_fingerprint import OKHTTP_UA

        return OKHTTP_UA

    def fingerprint_key(self) -> str:
        raw = f'{self.manufacturer}|{self.model}|{self.board}|{self.os_version}|{self.screen_w}x{self.screen_h}@{self.density}'
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def android_id_for(account_id: Any, model: str) -> str:
    """确定性 android_id：16 位小写 hex，账号内恒定、账号间不撞。"""
    digest = hashlib.sha256(f'{account_id}:{model}'.encode('utf-8')).hexdigest()
    return digest[:16]


def provision_device(
    account_id: Any,
    province: Optional[str] = None,
    *,
    key: Optional[str] = None,
    seed: Optional[str] = None,
    now_ms: Optional[int] = None,
    uptime_s: Optional[int] = None,
    pool: Optional[Mapping[str, DeviceProfile]] = None,
) -> TsnDeviceRecord:
    """确定性生成账号设备档案。

    ``seed`` 默认取 ``f'{account_id}:device'``：同一账号永远得到同一机型、
    同一分辨率、同一 android_id。``uptime_s`` 可显式给（复现历史会话时用），
    否则由 seed 决定一个 30min~30d 的开机时长。
    """
    import time

    device_key = key or str(account_id)
    device_pool = pool or load_device_pool()
    rng = random.Random(seed or f'{device_key}:device')
    profiles, weights = _weighted_profiles(province, device_pool)
    chosen = rng.choices(profiles, weights=weights, k=1)[0]
    os_version = rng.choice(list(chosen.os_versions))
    if uptime_s is None:
        uptime_s = rng.randint(UPTIME_MIN_S, UPTIME_MAX_S)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    android_id = android_id_for(device_key, chosen.model)
    return TsnDeviceRecord(
        device_pk=device_pk_for(device_key, android_id),
        account_id=str(account_id),
        android_id=android_id,
        manufacturer=chosen.manufacturer,
        model=chosen.model,
        board=chosen.board,
        os_version=os_version,
        abi=chosen.abi,
        screen_w=chosen.screen_w,
        screen_h=chosen.screen_h,
        density=chosen.density,
        boot_time_ms=int(now - uptime_s * 1000),
        bound_at=now,
        province=_normalize_province(province) or None,
    )


@dataclass
class DeviceIssue:
    code: str
    message: str
    severity: str = 'error'


_ANDROID_ID_RE = re.compile(r'^[0-9a-f]{16}$')


def validate_device_consistency(device: TsnDeviceRecord, *, now_ms: Optional[int] = None, pool: Optional[Mapping[str, DeviceProfile]] = None) -> List[DeviceIssue]:
    """物理自洽自检。返回空列表表示这台"手机"经得起人工复核。"""
    import time

    issues: List[DeviceIssue] = []
    catalog = pool or load_device_pool()
    now = now_ms if now_ms is not None else int(time.time() * 1000)

    profile = catalog.get(device.model)
    if profile is None:
        issues.append(DeviceIssue('model_unknown', f'机型 {device.model} 不在真实机型表内'))
    else:
        if profile.board != device.board:
            issues.append(DeviceIssue('board_mismatch', f'{device.model} 的 board 应为 {profile.board}，实际 {device.board}'))
        if profile.manufacturer != device.manufacturer:
            issues.append(DeviceIssue('manufacturer_mismatch', f'{device.model} 的厂商应为 {profile.manufacturer}，实际 {device.manufacturer}'))
        if (device.screen_w, device.screen_h, device.density) != (profile.screen_w, profile.screen_h, profile.density):
            issues.append(DeviceIssue('display_mismatch', '分辨率/密度与该机型公开参数不一致'))
        if device.os_version not in profile.os_versions:
            issues.append(DeviceIssue('os_not_in_profile', f'{device.model} 不发 {device.os_version}，允许 {list(profile.os_versions)}'))

    if not _ANDROID_ID_RE.match(device.android_id or ''):
        issues.append(DeviceIssue('android_id_format', 'android_id 必须是 16 位小写 hex'))
    elif device.android_id in ('0000000000000000', '9774d56d682e549c', 'ffffffffffffffff'):
        issues.append(DeviceIssue('android_id_known_bad', 'android_id 命中已知异常/默认值黑名单'))

    api = device.android_api
    if api == 0:
        issues.append(DeviceIssue('os_version_unparsable', f'os_version 无法解析: {device.os_version}'))
    elif api < APK_MIN_SDK:
        issues.append(DeviceIssue('apk_incompatible', f'Android {device.os_version}({api}) 低于 APK minSdk {APK_MIN_SDK}'))

    dp_w, dp_h = device.dp_metrics()
    if not 350 <= dp_w <= 460:
        issues.append(DeviceIssue('dp_width_outlier', f'dp 宽 {dp_w:.1f} 超出 350~460'))
    if not 600 <= dp_h <= 1000:
        issues.append(DeviceIssue('dp_height_outlier', f'dp 高 {dp_h:.1f} 超出 600~1000'))

    uptime_ms = now - int(device.boot_time_ms)
    if uptime_ms <= 0:
        issues.append(DeviceIssue('boot_time_future', f'boot_time_ms 在未来: {device.boot_time_ms} > {now}'))
    elif uptime_ms / 1000 < UPTIME_MIN_S:
        issues.append(DeviceIssue('uptime_too_short', f'开机仅 {uptime_ms / 1000:.0f}s，短时间内反复重启本身是特征'))
    elif uptime_ms / 1000 > UPTIME_MAX_S:
        issues.append(DeviceIssue('uptime_too_long', f'开机 {uptime_ms / 3600000:.0f}h，超过 30 天未见重启'))

    ua = device.webview_user_agent()
    if f'Android {device.os_version}; {device.model} ' not in ua:
        issues.append(DeviceIssue('ua_model_os_mismatch', 'UA 里的 Android 版本/机型与设备档案不一致'))

    return issues


def assert_device_consistent(device: TsnDeviceRecord, **kwargs: Any) -> None:
    issues = validate_device_consistency(device, **kwargs)
    errors = [i for i in issues if i.severity == 'error']
    if errors:
        detail = '; '.join(f'{i.code}: {i.message}' for i in errors)
        raise ValueError(f'设备指纹不自洽 -> {detail}')


def device_pk_for(device_key: Any, android_id: str) -> str:
    return hashlib.sha256(f'{device_key}:{android_id}'.encode('utf-8')).hexdigest()[:24]


def device_key_for(school_id: Any, username: str) -> str:
    """设备身份键：学校 + 学号。重授权、换库、换机器都不换设备。"""
    return f'{school_id}:{username}'


def from_row(row: Any) -> TsnDeviceRecord:
    """ORM 行 -> dataclass（避免业务层直接依赖 SQLAlchemy 实例）。"""
    return TsnDeviceRecord(
        device_pk=row.device_pk,
        account_id=str(row.account_id),
        android_id=row.android_id,
        manufacturer=row.manufacturer,
        model=row.model,
        board=row.board,
        os_version=row.os_version,
        abi=row.abi,
        screen_w=int(row.screen_w),
        screen_h=int(row.screen_h),
        density=int(row.density),
        boot_time_ms=int(row.boot_time_ms),
        apk_version=row.apk_version or APK_VERSION_NAME,
        bound_at=int(row.bound_at.timestamp() * 1000) if hasattr(row.bound_at, 'timestamp') and row.bound_at else 0,
        last_seen_ip=row.last_seen_ip,
        last_seen_lat=row.last_seen_lat,
        last_seen_lng=row.last_seen_lng,
        province=getattr(row, 'province', None),
    )


def to_row_kwargs(device: TsnDeviceRecord) -> Dict[str, Any]:
    return {
        'device_pk': device.device_pk,
        'account_id': device.account_id,
        'android_id': device.android_id,
        'manufacturer': device.manufacturer,
        'model': device.model,
        'board': device.board,
        'os_version': device.os_version,
        'abi': device.abi,
        'screen_w': device.screen_w,
        'screen_h': device.screen_h,
        'density': device.density,
        'boot_time_ms': device.boot_time_ms,
        'apk_version': device.apk_version,
        'last_seen_ip': device.last_seen_ip,
        'last_seen_lat': device.last_seen_lat,
        'last_seen_lng': device.last_seen_lng,
        'province': device.province,
    }


def rebind_os_version(device: TsnDeviceRecord, rng: random.Random) -> TsnDeviceRecord:
    """设备升级系统版本（真实用户会 OTA），仍限制在该机型的公开版本内。"""
    profile = DEVICE_CATALOG.get(device.model)
    if profile is None:
        return device
    return replace(device, os_version=rng.choice(list(profile.os_versions)))
