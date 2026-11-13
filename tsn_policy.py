"""工程与运营层：熔断、版本漂移、审计、金丝雀（方案 P2-2 / P2-3 / P2-5）。

这一层的价值不在于"多提交一次"，而在于**止损失败**：

* 签名错了还继续跑，只会把整批账号一起打过期版本特征；
* 被风控拒绝两次还换参数重试，等于把"这台设备 + 这个出口"焊死成可疑组合；
* APK 版本漂移后还按老协议提交，会产出一批"协议不匹配"的异常样本。

所以策略是：错误先分类，再决定是全局停、账号冷却、还是可以重试。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from loguru import logger

AUDIT_ROOT = Path('audit')


class Severity(str, Enum):
    FATAL = 'FATAL'
    ACCOUNT = 'ACCOUNT'
    TRANSIENT = 'TRANSIENT'


class Action(str, Enum):
    STOP_ALL = 'stop_all'
    COOLDOWN_24H = 'cooldown_24h'
    FREEZE_7D = 'freeze_7d'
    MANUAL_REVIEW = 'manual_review'
    RETRY_3 = 'retry_3'


#: 与方案原表一致；关键字命中即分类，未命中默认 TRANSIENT。
ERROR_CLASS: Dict[str, Tuple[Severity, Action]] = {
    '签名错误': (Severity.FATAL, Action.STOP_ALL),
    '数据不合格': (Severity.ACCOUNT, Action.COOLDOWN_24H),
    '风控拒绝': (Severity.ACCOUNT, Action.FREEZE_7D),
    '账号异常': (Severity.ACCOUNT, Action.MANUAL_REVIEW),
    '网络错误': (Severity.TRANSIENT, Action.RETRY_3),
}

#: 真实返回里这些措辞都等价于上面的类别。
_KEYWORD_ALIASES: Dict[str, Tuple[str, ...]] = {
    '签名错误': ('签名', 'sign', '验签', 'appsecret', 'appsign', '非法请求'),
    '数据不合格': ('不合格', '不符合', '无效记录', '数据异常', '配速', '步频', '里程', '时间异常', '超出范围'),
    '风控拒绝': ('风控', '风险', '异常行为', '作弊', 'cheat', '拒绝', '拦截'),
    '账号异常': ('账号异常', '冻结', '封禁', '禁用', '黑名单', '审核'),
    '网络错误': ('timeout', 'timed out', '连接', 'network', 'ssl', 'connection reset', '502', '503', '504'),
}

MAX_TRANSIENT_RETRIES = 3


@dataclass(frozen=True)
class ErrorRule:
    kind: str
    severity: Severity
    action: Action
    matched: str = ''

    @property
    def fatal(self) -> bool:
        return self.severity is Severity.FATAL


DEFAULT_RULE = ErrorRule(kind='未分类', severity=Severity.TRANSIENT, action=Action.RETRY_3)


def classify_error(message: Optional[str]) -> ErrorRule:
    """把服务端任意措辞映射到 5 个类别之一。"""
    text = (message or '').strip()
    lowered = text.lower()
    for kind, (severity, action) in ERROR_CLASS.items():
        if kind in text:
            return ErrorRule(kind=kind, severity=severity, action=action, matched=kind)
    for kind, words in _KEYWORD_ALIASES.items():
        for word in words:
            if word in lowered or word in text:
                severity, action = ERROR_CLASS[kind]
                return ErrorRule(kind=kind, severity=severity, action=action, matched=word)
    return DEFAULT_RULE


@dataclass
class AccountState:
    account_id: str
    strikes: int = 0
    frozen_until: float = 0.0
    reason: str = ''
    last_error: str = ''

    def blocked(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) < self.frozen_until

    def as_dict(self) -> Dict[str, Any]:
        return {
            'account_id': self.account_id,
            'strikes': self.strikes,
            'frozen_until': self.frozen_until,
            'reason': self.reason,
            'last_error': self.last_error,
        }


class CircuitBreaker:
    """账号级 + 全局级熔断。状态可以落盘，进程重启后仍然有效。"""

    def __init__(self, state_path: Optional[Path] = Path('audit') / 'breaker.json') -> None:
        self.state_path = state_path
        self.global_stop = False
        self.global_reason = ''
        self.accounts: Dict[str, AccountState] = {}
        self._load()

    # -- persistence -------------------------------------------------
    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            with open(self.state_path, 'r', encoding='utf-8') as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        self.global_stop = bool(raw.get('global_stop'))
        self.global_reason = str(raw.get('global_reason') or '')
        for item in raw.get('accounts', []):
            try:
                state = AccountState(
                    account_id=str(item['account_id']),
                    strikes=int(item.get('strikes') or 0),
                    frozen_until=float(item.get('frozen_until') or 0.0),
                    reason=str(item.get('reason') or ''),
                    last_error=str(item.get('last_error') or ''),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self.accounts[state.account_id] = state

    def save(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                'global_stop': self.global_stop,
                'global_reason': self.global_reason,
                'accounts': [s.as_dict() for s in self.accounts.values()],
                'updated_at': int(time.time()),
            }
            with open(self.state_path, 'w', encoding='utf-8') as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning(f'熔断状态落盘失败: {exc}')

    # -- decisions ---------------------------------------------------
    def state(self, account_id: Any) -> AccountState:
        key = str(account_id)
        if key not in self.accounts:
            self.accounts[key] = AccountState(account_id=key)
        return self.accounts[key]

    def allow(self, account_id: Any) -> Tuple[bool, str]:
        if self.global_stop:
            return (False, f'全局熔断: {self.global_reason}')
        state = self.state(account_id)
        now = time.time()
        if state.blocked(now):
            remain = int(state.frozen_until - now)
            return (False, f'账号冷却中，剩余 {remain}s（{state.reason}）')
        return (True, '')

    def record_success(self, account_id: Any) -> None:
        state = self.state(account_id)
        state.strikes = 0
        state.frozen_until = 0.0
        state.reason = ''
        self.save()

    def record_failure(self, account_id: Any, message: Optional[str]) -> ErrorRule:
        rule = classify_error(message)
        state = self.state(account_id)
        state.strikes += 1
        state.last_error = message or ''
        now = time.time()
        if rule.severity is Severity.FATAL:
            self.global_stop = True
            self.global_reason = f'{rule.kind}: {message}'
            logger.error(f'FATAL -> 全局停止：{self.global_reason}')
        elif rule.action is Action.COOLDOWN_24H:
            state.frozen_until = now + 24 * 3600
            state.reason = rule.kind
        elif rule.action is Action.FREEZE_7D:
            state.frozen_until = now + 7 * 24 * 3600
            state.reason = rule.kind
        elif rule.action is Action.MANUAL_REVIEW:
            state.frozen_until = now + 30 * 24 * 3600
            state.reason = rule.kind
        self.save()
        return rule

    def resume_global(self) -> None:
        self.global_stop = False
        self.global_reason = ''
        self.save()

    def snapshot(self) -> str:
        lines = [f'全局熔断: {"是" if self.global_stop else "否"} {self.global_reason}']
        for state in self.accounts.values():
            if state.strikes or state.blocked():
                lines.append(f'  - {state.account_id}: strikes={state.strikes} blocked={state.blocked()} {state.reason}')
        return '\n'.join(lines)


class RunMode(str, Enum):
    NORMAL = 'NORMAL'
    READ_ONLY = 'READ_ONLY'


@dataclass
class VersionGuard:
    """APK 版本漂移：不一致就只读，避免拿老协议去撞新签名。"""

    local_version: str = '2.0.23'
    on_alert: Optional[Callable[[str], None]] = None

    def check(self, remote_version: Optional[str]) -> RunMode:
        remote = (remote_version or '').strip()
        if not remote:
            return RunMode.READ_ONLY
        if remote != self.local_version:
            message = f'APK version drift: {self.local_version} -> {remote}'
            logger.error(message)
            if self.on_alert:
                self.on_alert(message)
            return RunMode.READ_ONLY
        return RunMode.NORMAL

    @staticmethod
    def extract_version(payload: Any) -> Optional[str]:
        """从 ``stuAppVersion2`` 一类响应里挖版本号。"""
        stack = [payload]
        keys = ('versionName', 'version', 'appVersion', 'version_name')
        seen = 0
        while stack and seen < 200:
            item = stack.pop()
            seen += 1
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in keys and isinstance(value, (str, int, float)):
                        return str(value)
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item[:20])
        return None


@dataclass
class AuditRecord:
    account_id: str
    stage: str
    ok: bool
    payload: Dict[str, Any] = field(default_factory=dict)
    error: str = ''
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def as_dict(self) -> Dict[str, Any]:
        return {
            'ts': self.ts_ms,
            'account_id': self.account_id,
            'stage': self.stage,
            'ok': self.ok,
            'error': self.error,
            'payload': self.payload,
        }


class AuditSink:
    """每次提交落盘 ``audit/YYYYMMDD/<account>.jsonl``。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or AUDIT_ROOT

    def path_for(self, account_id: str, day: Optional[dt.date] = None) -> Path:
        day = day or dt.date.today()
        return self.root / day.strftime('%Y%m%d') / f'{account_id}.jsonl'

    def write(self, record: AuditRecord) -> Path:
        path = self.path_for(record.account_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(record.as_dict(), ensure_ascii=False) + '\n')
        except OSError as exc:
            logger.warning(f'审计落盘失败: {exc}')
        return path

    def tail(self, account_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        path = self.path_for(account_id)
        if not path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return rows[-limit:]

    def recent_success_dates(self, account_id: str, limit_days: int = 60) -> List[str]:
        """从审计里恢复"这个账号最近跑了哪些天"，供活动日历使用。"""
        dates: List[str] = []
        for record in self.tail(account_id, limit=2000):
            if not record.get('ok') or record.get('stage') != 'submit':
                continue
            ts = record.get('ts', 0) / 1000.0
            try:
                dates.append(dt.datetime.fromtimestamp(ts).date().isoformat())
            except (OverflowError, OSError, ValueError):
                continue
        return sorted(set(dates))[-limit_days:]


@dataclass
class CanaryPlan:
    """每批先跑 1 个 canary，观察 24h 再放量。"""

    canary_accounts: Sequence[str] = ()
    observe_hours: float = 24.0

    def is_canary(self, account_id: Any) -> bool:
        return str(account_id) in {str(a) for a in self.canary_accounts}

    def gate(self, account_id: Any, canary_last_run_ms: Optional[int]) -> Tuple[bool, str]:
        if self.is_canary(account_id):
            return (True, 'canary')
        if canary_last_run_ms is None:
            return (False, 'canary 尚未跑过，本批不放量')
        elapsed_h = (time.time() * 1000 - canary_last_run_ms) / 3600000.0
        if elapsed_h < self.observe_hours:
            return (False, f'canary 观察期未满（{elapsed_h:.1f}h < {self.observe_hours:.0f}h）')
        return (True, 'canary passed')


def error_table_markdown() -> str:
    lines = ['| 关键字 | 级别 | 处置 |', '|---|---|---|']
    for kind, (severity, action) in ERROR_CLASS.items():
        lines.append(f'| {kind} | {severity.value} | {action.value} |')
    return '\n'.join(lines)


def audit_disk_usage(root: Optional[Path] = None) -> Dict[str, Any]:
    target = root or AUDIT_ROOT
    total = 0
    files = 0
    if target.exists():
        for dirpath, _dirnames, filenames in os.walk(target):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                    files += 1
                except OSError:
                    continue
    return {'files': files, 'bytes': total, 'mb': round(total / 1048576, 2)}
