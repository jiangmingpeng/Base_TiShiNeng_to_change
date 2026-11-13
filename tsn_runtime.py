"""账号运行时上下文：把设备、出口、HTTP 客户端绑成一个整体。

业务代码只调 :func:`build_runtime`，不再自己拼 deviceId / proxy / headers。
这一层是 P0-1 + P0-2 + P0-3 三条线的汇合点：

```
account ──> TsnDevice(确定性)     ──┐
        └─> EgressBinding(确定性) ──┼─> TsnHttpSession(指纹+代理)
                                    └─> SDK
```
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import select

import tsn_device
import tsn_egress
import tsn_fingerprint
from models import TsnDevice_Model, TsnEgressBinding_Model

#: 全局按出口节流器（P2-4）。qps<=0 表示关闭（不建议：同一出口高频请求本身就是特征）。
_EGRESS_QPS = float(os.getenv('TSN_EGRESS_QPS', '0.5'))
_LIMITER = tsn_egress.PerEgressRateLimiter(_EGRESS_QPS) if _EGRESS_QPS > 0 else None


@dataclass
class AccountRuntime:
    account_id: str
    device: tsn_device.TsnDeviceRecord
    egress: Optional[tsn_egress.Binding]
    http: tsn_fingerprint.TsnHttpSession

    @property
    def proxy(self) -> Optional[str]:
        return self.egress.url if self.egress else None

    @property
    def egress_key(self) -> str:
        return self.egress.egress_key if self.egress else 'direct'

    def summary(self) -> Dict[str, Any]:
        return {
            'account_id': self.account_id,
            'device': f'{self.device.manufacturer} {self.device.model} / Android {self.device.os_version}',
            'android_id': self.device.android_id,
            'egress': self.egress.masked if self.egress else 'direct(不安全)',
            'http_backend': self.http.backend_name,
        }

    async def aclose(self) -> None:
        await self.http.aclose()


async def _load_device(db, account_id: str) -> Optional[TsnDevice_Model]:
    stmt = select(TsnDevice_Model).where(TsnDevice_Model.account_id == account_id)
    result = await db.execute(stmt)
    return result.scalars().first()


async def ensure_device(account, db, *, now_ms: Optional[int] = None) -> tsn_device.TsnDeviceRecord:
    """取账号的设备档案；不存在则确定性生成并落库。"""
    account_id = str(account.id)
    row = await _load_device(db, account_id)
    if row is not None:
        record = tsn_device.from_row(row)
        issues = tsn_device.validate_device_consistency(record, now_ms=now_ms)
        errors = [i for i in issues if i.severity == 'error']
        if errors:
            # 已落库的档案不自洽（例如机型表更新了）→ 重新生成，宁可换机也不要留着被聚类
            logger.error(f'账号 {account_id} 设备档案不自洽，重新生成: {errors[0].message}')
        else:
            return record
    province = getattr(getattr(account, 'school', None), 'province_name', None)
    device_key = tsn_device.device_key_for(getattr(account, 'school_id', ''), getattr(account, 'username', account_id))
    record = tsn_device.provision_device(account_id, province, key=device_key, now_ms=now_ms)
    tsn_device.assert_device_consistent(record, now_ms=now_ms)
    if row is None:
        db.add(TsnDevice_Model(**tsn_device.to_row_kwargs(record)))
    else:
        for key, value in tsn_device.to_row_kwargs(record).items():
            if key != 'device_pk':
                setattr(row, key, value)
    await db.flush()
    logger.info(f'账号 {account_id} 绑定设备 {record.manufacturer} {record.model} android_id={record.android_id}')
    return record


async def ensure_egress(account, db, pool: Optional[tsn_egress.EgressPool] = None) -> Optional[tsn_egress.Binding]:
    """取账号的出口绑定；不存在则按省份确定性分配并落库。"""
    account_id = str(account.id)
    school = getattr(account, 'school', None)
    province = getattr(school, 'province_name', None)
    pool = pool or tsn_egress.EgressPool.load()

    stmt = select(TsnEgressBinding_Model).where(TsnEgressBinding_Model.account_id == account_id)
    result = await db.execute(stmt)
    row = result.scalars().first()
    if row is not None:
        if row.polluted:
            logger.error(f'账号 {account_id} 的出口组合已被判定污染，需要换出口再跑')
            return None
        for egress in pool.egresses:
            if egress.key == row.egress_key:
                return tsn_egress.Binding(account_id=account_id, egress_key=egress.key, url=egress.url, province=egress.province)
        logger.warning(f'账号 {account_id} 的历史出口不在当前池里，重新分配')

    existing: Dict[str, str] = {}
    for candidate in (await db.execute(select(TsnEgressBinding_Model))).scalars().all():
        existing[str(candidate.account_id)] = candidate.egress_key
    binding = tsn_egress.bind_egress(account_id, province, pool, existing=existing)
    if binding is None:
        logger.warning(f'省份 {province} 没有可用出口，本次将直连（D1/D2 必中）')
        return None
    if row is None:
        db.add(TsnEgressBinding_Model(account_id=account_id, egress_key=binding.egress_key, province=binding.province))
    else:
        row.egress_key = binding.egress_key
        row.province = binding.province
    await db.flush()
    logger.info(f'账号 {account_id} 绑定出口 {binding.masked}（{binding.province}）')
    return binding


async def build_runtime(account, db, *, pool: Optional[tsn_egress.EgressPool] = None, now_ms: Optional[int] = None) -> AccountRuntime:
    device = await ensure_device(account, db, now_ms=now_ms)
    binding = await ensure_egress(account, db, pool=pool)
    http = tsn_fingerprint.TsnHttpSession(
        proxy=binding.url if binding else None,
        rate_limiter=_LIMITER,
        egress_key=binding.egress_key if binding else 'direct',
    )
    return AccountRuntime(account_id=str(account.id), device=device, egress=binding, http=http)


async def touch_device(account, db, *, ip: Optional[str] = None, lat: Optional[float] = None, lng: Optional[float] = None) -> None:
    """记录最近一次看到的出口 IP / 坐标，用于事后审计与漂移检测。"""
    row = await _load_device(db, str(account.id))
    if row is None:
        return
    if ip:
        if row.last_seen_ip and row.last_seen_ip != ip:
            logger.warning(f'账号 {account.id} 出口 IP 漂移: {row.last_seen_ip} -> {ip}')
        row.last_seen_ip = ip
    if lat is not None:
        row.last_seen_lat = lat
    if lng is not None:
        row.last_seen_lng = lng
    await db.flush()


def describe_runtime(runtime: AccountRuntime) -> str:
    info = runtime.summary()
    lines = [f'账号 {info["account_id"]} 运行时:']
    lines.append(f'  设备: {info["device"]}  android_id={info["android_id"]}')
    lines.append(f'  出口: {info["egress"]}')
    lines.append(f'  HTTP: {info["http_backend"]}')
    issues = tsn_device.validate_device_consistency(runtime.device)
    if issues:
        lines.append('  设备问题:')
        lines.extend(f'    ! {i.code}: {i.message}' for i in issues)
    return '\n'.join(lines)
