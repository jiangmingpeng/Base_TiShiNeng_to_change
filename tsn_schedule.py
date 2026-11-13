"""时序与行为节律（方案 P1-6 / P1-7 / P1-8）。

平台侧最容易算的三个统计量：

* **提交时刻熵**：真人从"跑完"到"点提交"是一段 log-normal 分布的延迟，
  而且是任意秒数；程序化提交常常整分、整 5 秒、或者固定 +100ms。
* **连续全勤**：真人每周 3~5 次，且不会连续 7 天不间断；考试周、寒暑假
  会明显掉下来。
* **开始时间分布**：晨跑真实分布是双峰（6:25 / 7:05 两个上课前的窗口），
  单峰窄分布本身就是机器特征。

本模块只做纯函数 + 日历判定，不碰网络，方便单测。
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

#: 学期考试/假期窗口（月, 日）。可按学校实际校历覆盖。
EXAM_WINDOWS: Tuple[Tuple[int, int, int, int], ...] = (
    (1, 5, 1, 20),
    (6, 20, 7, 10),
    (12, 25, 1, 5),
)
VACATION_MONTHS: Tuple[int, ...] = (1, 2, 7, 8)


def is_exam_window(day: dt.date) -> bool:
    for start_m, start_d, end_m, end_d in EXAM_WINDOWS:
        start = dt.date(day.year, start_m, start_d)
        end = dt.date(day.year if end_m >= start_m else day.year + 1, end_m, end_d)
        if start <= day <= end:
            return True
    return False


def is_vacation(day: dt.date) -> bool:
    return day.month in VACATION_MONTHS


def week_key(day: dt.date) -> Tuple[int, int]:
    iso = day.isocalendar()
    return (iso.year, iso.week)


def consecutive_days(history: Iterable[dt.date], day: dt.date) -> int:
    """``day`` 之前连续跑了几天（不含 ``day``）。"""
    done: Set[dt.date] = {d for d in history if d < day}
    count = 0
    cursor = day - dt.timedelta(days=1)
    while cursor in done:
        count += 1
        cursor -= dt.timedelta(days=1)
    return count


def week_count(history: Iterable[dt.date], day: dt.date) -> int:
    target = week_key(day)
    return sum(1 for d in history if week_key(d) == target and d < day)


def weekly_target(account_id: object, day: dt.date) -> int:
    """同一账号同一周的频次目标是确定的（3/4/5 次）。"""
    rng = random.Random(f'{account_id}:{week_key(day)}:weekly')
    return rng.choices([3, 4, 5], weights=[0.30, 0.45, 0.25], k=1)[0]


def should_run(
    account_id: object,
    day: dt.date,
    history: Sequence[dt.date] = (),
    *,
    exam: Optional[bool] = None,
    vacation: Optional[bool] = None,
) -> bool:
    """当天是否应该跑。返回 True 只是"候选"，还要过体力/配速约束。"""
    rng = random.Random(f'{account_id}:{day.isoformat()}')
    exam = is_exam_window(day) if exam is None else exam
    vacation = is_vacation(day) if vacation is None else vacation
    if exam or vacation:
        return rng.random() < 0.15
    streak = consecutive_days(history, day)
    if streak >= 4:
        # 连续 4 天以上几乎必然休息（真人不会 5 连跑）
        return rng.random() < 0.12
    if streak >= 3:
        # 连续 3 天之后大概率休一天
        return rng.random() < 0.30
    return week_count(history, day) < weekly_target(account_id, day)


def plan_calendar(account_id: object, start: dt.date, end: dt.date, history: Sequence[dt.date] = ()) -> List[dt.date]:
    """生成一段时间的跑步日历（按时间顺序，动态吃 history）。"""
    planned: List[dt.date] = []
    known = list(history)
    cursor = start
    while cursor <= end:
        if should_run(account_id, cursor, known):
            planned.append(cursor)
            known.append(cursor)
        cursor += dt.timedelta(days=1)
    return planned


#: 各跑步类型的开始时间分布：高斯混合（权重, 均值分钟, 标准差分钟）。
START_MINUTE_MIXTURES: Dict[str, Tuple[Tuple[float, float, float], ...]] = {
    'morning': ((0.62, 385.0, 12.0), (0.38, 425.0, 15.0)),  # 06:25 / 07:05
    'sun': ((0.55, 960.0, 35.0), (0.45, 1050.0, 40.0)),  # 16:00 / 17:30
    'freedom': ((0.5, 1035.0, 55.0), (0.5, 1170.0, 60.0)),  # 17:15 / 19:30
}


def pick_start_minute(run_type: str, rng: Optional[random.Random] = None) -> int:
    """返回当天 0 点起的分钟数（本地时间）。"""
    rng = rng or random.Random()
    mixture = START_MINUTE_MIXTURES.get(run_type, START_MINUTE_MIXTURES['freedom'])
    weights = [w for w, _, _ in mixture]
    _, mu, sigma = rng.choices(list(mixture), weights=weights, k=1)[0]
    minute = int(round(rng.gauss(mu, sigma)))
    return max(300, min(1380, minute))  # 05:00 ~ 23:00


def pick_start_datetime(day: dt.date, run_type: str, rng: Optional[random.Random] = None) -> dt.datetime:
    rng = rng or random.Random()
    minute = pick_start_minute(run_type, rng)
    second = rng.randint(0, 59)
    return dt.datetime.combine(day, dt.time(hour=minute // 60, minute=minute % 60, second=second))


def submit_delay_s(rng: Optional[random.Random] = None) -> float:
    """跑完到提交的延迟：log-normal(4.3, 0.5) 中位 ~74s，再加 0~59s。

    最后这一步保证提交时刻几乎不会落在整分上。
    """
    rng = rng or random.Random()
    base = rng.lognormvariate(4.3, 0.5)
    return base + rng.uniform(0.0, 59.0)


def submit_timestamp_ms(finish_ms: int, rng: Optional[random.Random] = None) -> int:
    return int(finish_ms + submit_delay_s(rng) * 1000)


def request_gap_s(rng: Optional[random.Random] = None) -> float:
    """同一会话内两个接口之间的间隔：log-normal 200~900ms。"""
    rng = rng or random.Random()
    return min(0.9, max(0.2, rng.lognormvariate(math.log(0.45), 0.35)))


@dataclass
class CalendarState:
    """账号的跑步历史（从审计日志/数据库重建）。"""

    account_id: str
    dates: List[dt.date] = field(default_factory=list)

    def add(self, day: dt.date) -> None:
        if day not in self.dates:
            self.dates.append(day)
            self.dates.sort()

    @classmethod
    def from_iso(cls, account_id: str, values: Iterable[str]) -> 'CalendarState':
        state = cls(account_id=str(account_id))
        for value in values:
            try:
                state.add(dt.date.fromisoformat(value))
            except ValueError:
                continue
        return state

    def next_candidates(self, start: dt.date, days: int) -> List[dt.date]:
        return plan_calendar(self.account_id, start, start + dt.timedelta(days=days - 1), self.dates)
