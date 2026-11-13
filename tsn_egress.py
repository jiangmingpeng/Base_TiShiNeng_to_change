"""出口 IP 管理：地理强一致绑定 + 按出口节流 + 污染组合登记（方案 P0-1 / P2-4）。

三条硬规则：

1. 出口归属地必须等于学校所在省份，ISP 类型必须是家宽/移动，IDC 一律排除；
2. 一个账号的出口在会话内不得漂移（``bind`` 是确定性的）；
3. 一个出口最多承载 3 个账号（真实 4G 出口背后有数百用户，3 个不会形成
   "同 IP 多账号" 的强聚类）。

关于 Device-as-Proxy 的一个常见错误：``adb reverse tcp:8888 tcp:8888``
是"手机 → PC"方向（手机访问自己的 8888 会打到 PC 的 8888），PC 走它反而
会把流量绕回 PC 的公网出口。要让 PC 借用手机的 4G 出口，方向必须是
``adb forward tcp:8888 tcp:8888``（PC 的 localhost:8888 → 手机 8888 上的
代理监听），:func:`device_as_proxy_commands` 生成的就是正确方向。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from loguru import logger

EGRESS_POOL_FILE = os.getenv('TSN_EGRESS_POOL_FILE', 'tsn_egress.json')
MAX_ACCOUNTS_PER_EGRESS = 3
DEFAULT_QPS_PER_EGRESS = 0.5  # 每出口 2 秒 1 请求
ALLOWED_ISPS = ('residential', 'mobile', 'broadband', 'cable', 'dsl', 'cellular')
FORBIDDEN_ISPS = ('idc', 'hosting', 'datacenter', 'cloud', 'vps')


@dataclass(frozen=True)
class Egress:
    """一条出口。``url`` 形如 ``http://user:pass@1.2.3.4:8080``。"""

    url: str
    province: str
    isp: str = 'residential'
    label: str = ''
    carrier: str = ''

    @property
    def key(self) -> str:
        return hashlib.sha256(self.url.encode('utf-8')).hexdigest()[:12]

    @property
    def masked(self) -> str:
        if '@' in self.url:
            scheme, rest = self.url.split('://', 1)
            _, host = rest.split('@', 1)
            return f'{scheme}://***@{host}'
        return self.url

    def is_compliant(self) -> bool:
        return self.isp.lower() in ALLOWED_ISPS and self.isp.lower() not in FORBIDDEN_ISPS


def normalize_province(name: Optional[str]) -> str:
    text = (name or '').strip()
    for suffix in ('省', '市', '自治区', '壮族', '回族', '维吾尔'):
        text = text.replace(suffix, '')
    return text


@dataclass
class EgressPool:
    egresses: List[Egress] = field(default_factory=list)

    @classmethod
    def load(cls, path: Optional[str] = None) -> 'EgressPool':
        target = path or EGRESS_POOL_FILE
        if not target or not os.path.exists(target):
            return cls([])
        try:
            with open(target, 'r', encoding='utf-8') as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.warning(f'出口池配置读取失败 {target}: {exc}')
            return cls([])
        items = raw.get('egresses', []) if isinstance(raw, dict) else raw
        pool: List[Egress] = []
        for item in items:
            try:
                pool.append(
                    Egress(
                        url=str(item['url']),
                        province=normalize_province(item.get('province')),
                        isp=str(item.get('isp') or 'residential'),
                        label=str(item.get('label') or ''),
                        carrier=str(item.get('carrier') or ''),
                    )
                )
            except (KeyError, TypeError):
                continue
        return cls(pool)

    def for_province(self, province: Optional[str]) -> List[Egress]:
        target = normalize_province(province)
        if not target:
            return [e for e in self.egresses if e.is_compliant()]
        exact = [e for e in self.egresses if e.province == target and e.is_compliant()]
        if exact:
            return exact
        # 省内没有符合类型的出口时，宁可返回空也不要跨省漂移
        return []

    def audit(self, province: Optional[str] = None) -> List[str]:
        """出口池体检：返回人可读的问题列表（问题要能定位到具体出口）。"""
        problems: List[str] = []
        target = normalize_province(province) if province is not None else ''
        scope = [e for e in self.egresses if not target or e.province == target]
        label = target or '*'
        if not scope:
            problems.append(f'省份 {label} 下没有配置任何出口')
            return problems
        compliant = [e for e in scope if e.is_compliant()]
        for egress in scope:
            if not egress.is_compliant():
                problems.append(f'{egress.masked} ISP={egress.isp} 属于 IDC/云厂商，D1 直接命中，已排除')
        if not compliant:
            problems.append(f'省份 {label} 下没有合规出口（家宽/移动）')
        elif len(compliant) == 1:
            problems.append(f'省份 {label} 只有 1 条合规出口，多账号会形成强聚类')
        return problems


@dataclass
class Binding:
    account_id: str
    egress_key: str
    url: str
    province: str
    bound_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def masked(self) -> str:
        """日志用：隐去代理账号密码。"""
        if '@' in self.url:
            scheme, rest = self.url.split('://', 1)
            _, host = rest.split('@', 1)
            return f'{scheme}://***@{host}'
        return self.url


def bind_egress(
    account_id: Any,
    province: Optional[str],
    pool: EgressPool,
    *,
    existing: Optional[Mapping[str, str]] = None,
    max_per_egress: int = MAX_ACCOUNTS_PER_EGRESS,
) -> Optional[Binding]:
    """给账号确定性分配出口。

    ``existing`` 是 ``account_id -> egress_key`` 的历史绑定，保持不变；
    新账号按 ``sha256(account_id) % len(pool)`` 落桶，并在超出
    ``max_per_egress`` 时线性探测下一个出口。
    """
    candidates = pool.for_province(province)
    if not candidates:
        return None
    candidates = sorted(candidates, key=lambda e: e.key)
    existing = existing or {}
    account_key = str(account_id)

    if account_key in existing:
        target_key = existing[account_key]
        for egress in candidates:
            if egress.key == target_key:
                return Binding(account_id=account_key, egress_key=egress.key, url=egress.url, province=egress.province)

    load: Dict[str, int] = {e.key: 0 for e in candidates}
    for key in existing.values():
        if key in load:
            load[key] += 1
    digest = hashlib.sha256(account_key.encode('utf-8')).hexdigest()
    start = int(digest[:8], 16) % len(candidates)
    for step in range(len(candidates)):
        egress = candidates[(start + step) % len(candidates)]
        if load[egress.key] < max_per_egress:
            return Binding(account_id=account_key, egress_key=egress.key, url=egress.url, province=egress.province)
    logger.warning(f'省份 {province} 出口已满（每出口上限 {max_per_egress} 账号），需要扩池')
    return None


def device_as_proxy_commands(port: int = 8888, serial: Optional[str] = None) -> List[str]:
    """Device-as-Proxy 的正确命令序列（方向容易写反，注释里说明）。"""
    target = f'-s {serial} ' if serial else ''
    return [
        f'adb {target}forward --remove tcp:{port}',
        # PC 的 8888 -> 手机的 8888（手机侧 Every Proxy / Postern 监听）
        f'adb {target}forward tcp:{port} tcp:{port}',
        # 关掉手机系统代理，避免 settings global http_proxy 暴露 proxy=true
        f'adb {target}shell settings put global http_proxy :0',
        f'adb {target}shell settings delete global global_http_proxy_host',
        f'adb {target}shell settings delete global global_http_proxy_port',
        f'# 自检：PC 端 curl -x http://127.0.0.1:{port} https://ipinfo.io/json 应为手机运营商地址',
    ]


def device_as_commands_preview(port: int = 8888) -> List[str]:
    """自检用的命令预览（不含真实 serial）。"""
    return [cmd for cmd in device_as_proxy_commands(port=port) if not cmd.startswith('#')]


class PerEgressRateLimiter:
    """按出口的令牌桶：同一出口两次请求至少间隔 ``1/max_qps`` 秒。

    另外在允许的间隔内再叠加 200~900ms 的 log-normal 抖动，避免出现
    "精确每 2.000 秒一次" 这种机器节律。
    """

    def __init__(self, max_qps_per_ip: float = DEFAULT_QPS_PER_EGRESS, jitter_ms: Tuple[float, float] = (200.0, 900.0)) -> None:
        if max_qps_per_ip <= 0:
            raise ValueError('max_qps_per_ip 必须为正')
        self.max_qps = float(max_qps_per_ip)
        self.min_interval_s = 1.0 / self.max_qps
        self.jitter_ms = jitter_ms
        self._last: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def acquire(self, egress_key: str, rng: Optional[random.Random] = None) -> float:
        rng = rng or random.Random()
        lock = self._lock(egress_key)
        async with lock:
            now = time.monotonic()
            last = self._last.get(egress_key, 0.0)
            wait = max(0.0, self.min_interval_s - (now - last))
            jitter = 0.0
            low, high = self.jitter_ms
            if high > 0:
                # log-normal 近似：中位数落在区间中点
                mu = (low + high) / 2 / 1000.0
                jitter = abs(rng.lognormvariate(0.0, 0.45)) * mu
            total = wait + jitter
            if total > 0:
                await asyncio.sleep(total)
            self._last[egress_key] = time.monotonic()
            return total


class PollutionRegistry:
    """记录"设备指纹 + 出口"组合的污染状态。

    单账号连续 2 次风控拒绝即判该组合已污染：这台机器这个 IP 以后再
    怎么改参数都会被关联，必须整组报废而不是继续硬撞。
    """

    def __init__(self, threshold: int = 2) -> None:
        self.threshold = threshold
        self._strikes: Dict[str, int] = {}

    @staticmethod
    def key(device_pk: str, egress_key: str) -> str:
        return f'{device_pk}@{egress_key}'

    def strike(self, device_pk: str, egress_key: str) -> int:
        k = self.key(device_pk, egress_key)
        self._strikes[k] = self._strikes.get(k, 0) + 1
        return self._strikes[k]

    def polluted(self, device_pk: str, egress_key: str) -> bool:
        return self._strikes.get(self.key(device_pk, egress_key), 0) >= self.threshold

    def snapshot(self) -> Dict[str, int]:
        return dict(self._strikes)


async def probe_egress(proxy: Optional[str], timeout: float = 10.0) -> Dict[str, Any]:
    """校验出口归属地：返回 ``{ip, country, region, city, org}``。

    直连（``proxy=None``）会被标记 ``direct=True`` —— 直连在 D1 上必中。
    """
    import httpx

    url = 'https://ipinfo.io/json'
    try:
        async with httpx.AsyncClient(proxy=proxy or None, timeout=timeout, trust_env=False) as client:
            resp = await client.get(url)
            data = resp.json() if resp.status_code == 200 else {}
    except Exception as exc:
        return {'error': str(exc), 'direct': proxy is None}
    data = dict(data or {})
    data['direct'] = proxy is None
    return data


def egress_report(pool: EgressPool, province: Optional[str]) -> str:
    candidates = pool.for_province(province)
    lines = [f'省份: {normalize_province(province) or "(未设置)"}', f'可用出口: {len(candidates)}']
    for egress in candidates:
        lines.append(f'  - {egress.masked} isp={egress.isp} label={egress.label or "-"}')
    problems = pool.audit(province)
    if problems:
        lines.append('问题:')
        lines.extend(f'  ! {p}' for p in problems)
    return '\n'.join(lines)
