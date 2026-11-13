import datetime as dt
import random

import tsn_schedule as sch


def test_is_exam_window():
    assert sch.is_exam_window(dt.date(2026, 6, 25))
    assert not sch.is_exam_window(dt.date(2026, 4, 15))


def test_submit_delay_never_lands_on_whole_minute():
    rng = random.Random(1)
    offsets = [sch.submit_delay_s(rng) for _ in range(200)]
    assert all(15 < d < 400 for d in offsets), (min(offsets), max(offsets))
    # 中位应当落在 60~120s（log-normal(4.3,0.5) 中位 73.7 + 0~59 均匀）
    median = sorted(offsets)[len(offsets) // 2]
    assert 90 < median < 140, median


def test_start_minute_bimodal_for_morning():
    rng = random.Random(2)
    minutes = [sch.pick_start_minute('morning', rng) for _ in range(600)]
    early = [m for m in minutes if m < 405]
    late = [m for m in minutes if m >= 405]
    assert 0.4 < len(early) / len(minutes) < 0.85
    assert 380 <= sum(early) / len(early) <= 395
    assert 415 <= sum(late) / len(late) <= 435


def test_start_minute_within_day_bounds():
    rng = random.Random(3)
    for _ in range(200):
        minute = sch.pick_start_minute('freedom', rng)
        assert 300 <= minute <= 1380


def test_weekly_target_in_range():
    for account in range(10):
        target = sch.weekly_target(account, dt.date(2026, 3, 10))
        assert target in (3, 4, 5)


def test_should_run_is_deterministic_per_day():
    day = dt.date(2026, 3, 12)
    history = [dt.date(2026, 3, 9), dt.date(2026, 3, 10), dt.date(2026, 3, 11)]
    first = sch.should_run('acc', day, history)
    assert first == sch.should_run('acc', day, history)
    # 在"已经连续跑了 3 天"的场景下，不同日期应当出现不同结论
    results = set()
    for i in range(60):
        day = dt.date(2026, 3, 1) + dt.timedelta(days=i)
        hist = [day - dt.timedelta(days=1), day - dt.timedelta(days=2), day - dt.timedelta(days=3)]
        results.add(sch.should_run('acc', day, hist))
    assert results == {True, False}


def test_should_run_stops_after_week_cap():
    day = dt.date(2026, 3, 12)  # 周四
    monday = dt.date(2026, 3, 9)
    target = sch.weekly_target('acc2', day)
    history = [monday + dt.timedelta(days=i) for i in range(5)]
    if sch.week_count(history, dt.date(2026, 3, 13)) >= target:
        assert sch.should_run('acc2', dt.date(2026, 3, 13), history) is False


def test_vacation_and_exam_reduce_probability():
    day = dt.date(2026, 7, 15)
    hits = sum(sch.should_run(f'acc{i}', day + dt.timedelta(days=i % 30)) for i in range(300))
    assert hits / 300 < 0.35, hits / 300


def test_plan_calendar_never_exceeds_four_consecutive():
    planned = sch.plan_calendar('acc', dt.date(2026, 3, 1), dt.date(2026, 4, 1))
    for day in planned:
        streak = 1
        cursor = day
        while cursor - dt.timedelta(days=1) in planned:
            streak += 1
            cursor -= dt.timedelta(days=1)
        assert streak <= 5, (day, streak)


def test_request_gap_in_range():
    rng = random.Random(4)
    gaps = [sch.request_gap_s(rng) for _ in range(100)]
    assert all(0.19 <= g <= 0.91 for g in gaps)


def test_calendar_state_from_iso():
    state = sch.CalendarState.from_iso('1', ['2026-03-01', 'bad-date', '2026-03-03'])
    assert state.dates == [dt.date(2026, 3, 1), dt.date(2026, 3, 3)]
    candidates = state.next_candidates(dt.date(2026, 3, 4), 7)
    assert all(isinstance(d, dt.date) for d in candidates)
