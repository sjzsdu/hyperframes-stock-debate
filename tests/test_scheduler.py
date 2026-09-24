"""排期引擎测试：窗口解析、窗口内随机、错过即认、节流与退避、闲置不写盘。"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from stocktalk.modules.scheduler import (
    DONE,
    FAILED,
    MISSED,
    PENDING,
    RUNNING,
    Plan,
    ScheduleConfigError,
    Scheduler,
    Window,
    build_plans,
    decide,
    expire,
    parse_window,
    parse_windows,
)

# ---------------------------------------------------------------------------
# 窗口解析
# ---------------------------------------------------------------------------


def test_parse_window_accepts_the_separators_people_actually_type() -> None:
    for raw in ("07:30-09:30", "7:30~9:30", "07:30—09:30", "07:30 至 09:30"):
        window = parse_window(raw)
        assert (window.start, window.end) == (time(7, 30), time(9, 30)), raw


def test_parse_window_rejects_overnight_windows() -> None:
    """跨午夜的窗口没法表达「今天的哪一档」，直接拒绝比默默算错好。"""
    with pytest.raises(ScheduleConfigError, match="跨了午夜"):
        parse_window("23:00-01:00")


def test_parse_window_rejects_equal_ends_and_garbage() -> None:
    with pytest.raises(ScheduleConfigError):
        parse_window("09:00-09:00")
    with pytest.raises(ScheduleConfigError):
        parse_window("早上")


def test_parse_windows_rejects_overlaps() -> None:
    with pytest.raises(ScheduleConfigError, match="重叠"):
        parse_windows(["07:30-09:30", "09:00-11:00"])


def test_parse_windows_sorts_by_start() -> None:
    windows = parse_windows(["18:30-21:00", "07:30-09:30"])
    assert [w.label for w in windows] == ["07:30-09:30", "18:30-21:00"]


# ---------------------------------------------------------------------------
# 窗口内随机
# ---------------------------------------------------------------------------


def test_pick_always_lands_inside_the_window() -> None:
    window = Window(time(7, 30), time(9, 30))
    rng = random.Random(7)
    for _ in range(200):
        moment = window.pick(date(2026, 9, 24), rng)
        assert date(2026, 9, 24) == moment.date()
        assert time(7, 30) <= moment.time() <= time(9, 30)


def test_pick_is_not_the_same_time_every_day() -> None:
    """这就是「不要每天都 08:30:00」的全部实现 —— 时刻必须真的在变。"""
    window = Window(time(7, 30), time(9, 30))
    rng = random.Random(1)
    moments = {window.pick(date(2026, 9, day), rng).time() for day in range(1, 21)}
    assert len(moments) > 10


def test_pick_leaves_room_before_the_window_closes() -> None:
    """取到窗口最后一分钟等于自找「差一点就过期」——唤醒、起子进程都要花几秒。

    窗口是唯一的容错来源，随机点必须留在窗口里面一点。
    """
    window = Window(time(7, 30), time(9, 30))
    rng = random.Random(7)
    latest = max(window.pick(date(2026, 9, 24), rng).time() for _ in range(500))
    assert latest <= time(9, 20)


def test_build_plans_is_idempotent_for_the_same_day() -> None:
    """守护进程会被 launchd 反复重启，重启不该重掷骰子（否则时刻漂移、还可能重发）。"""
    day = date(2026, 9, 24)
    windows = parse_windows(["07:30-09:30", "18:30-21:00"])
    first = build_plans(day, windows, random.Random(3))
    second = build_plans(day, windows, random.Random(99), existing=first)
    assert [p.planned_at for p in second] == [p.planned_at for p in first]


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------


def _plan(day: str, window: str, planned_at: str, **kwargs) -> Plan:
    return Plan(day=day, window=window, planned_at=planned_at, **kwargs)


def test_decide_runs_when_due() -> None:
    plan = _plan("2026-09-24", "07:30-09:30", "2026-09-24T08:12:00")
    result = decide([plan], datetime(2026, 9, 24, 8, 12), retry_minutes=45)
    assert result.plan is plan
    assert result.reason == "到点"


def test_decide_waits_before_the_window() -> None:
    plan = _plan("2026-09-24", "07:30-09:30", "2026-09-24T08:12:00")
    result = decide([plan], datetime(2026, 9, 24, 6, 0), retry_minutes=45)
    assert result.plan is None
    assert result.next_at == datetime(2026, 9, 24, 8, 12)


def test_decide_still_runs_a_plan_the_machine_woke_up_late_for() -> None:
    """唤醒晚了十分钟照发：窗口内任何时刻都成立，这是唯一的容错来源。

    窗口外的情况不归这里管 —— 那种档位在 decide 之前就已经被 expire 标掉了。
    """
    plan = _plan("2026-09-24", "07:30-09:30", "2026-09-24T08:12:00")
    result = decide([plan], datetime(2026, 9, 24, 9, 20), retry_minutes=45)
    assert result.plan is plan


def test_decide_backs_off_after_a_failure() -> None:
    plan = _plan("2026-09-24", "07:30-09:30", "2026-09-24T08:00:00",
                 attempts=1, started_at="2026-09-24T08:00:00", finished_at="2026-09-24T08:30:00")
    result = decide([plan], datetime(2026, 9, 24, 9, 0), retry_minutes=45)
    assert result.plan is None
    assert result.next_at == datetime(2026, 9, 24, 9, 15)


def test_a_retry_is_never_pulled_earlier_than_the_slot() -> None:
    """退避算出来的时刻可能早于计划时刻（失败得很快），那就按计划时刻走。"""
    plan = _plan("2026-09-24", "07:30-09:30", "2026-09-24T08:00:00",
                 attempts=1, started_at="2026-09-24T07:00:00", finished_at="2026-09-24T07:05:00")
    assert plan.retry_ready_at(45) == datetime(2026, 9, 24, 8, 0)


def test_decide_ignores_plans_that_already_ran() -> None:
    plan = _plan("2026-09-24", "07:30-09:30", "2026-09-24T08:00:00", status=DONE)
    result = decide([plan], datetime(2026, 9, 24, 12, 0), retry_minutes=45)
    assert result.plan is None


# ---------------------------------------------------------------------------
# 过期：窗口就是硬边界
# ---------------------------------------------------------------------------


def test_expire_writes_off_a_slot_once_its_window_closes() -> None:
    """过期即认，不做事后补发 —— 迟到的时刻比不发更假，还会搅乱当天的节奏。"""
    morning = _plan("2026-09-24", "07:30-09:30", "2026-09-24T08:00:00")
    evening = _plan("2026-09-24", "18:30-21:00", "2026-09-24T19:00:00")

    assert expire([morning, evening], datetime(2026, 9, 24, 9, 29)) == []

    dropped = expire([morning, evening], datetime(2026, 9, 24, 9, 31))
    assert dropped == [morning]
    assert morning.status == MISSED
    assert "窗口已过" in morning.note
    assert evening.status == PENDING


def test_expire_remembers_a_slot_that_had_already_been_tried() -> None:
    """「试了两次都没成」和「压根没轮到」是两回事，状态上要分得清。"""
    tried = _plan("2026-09-24", "07:30-09:30", "2026-09-24T08:00:00",
                  attempts=2, finished_at="2026-09-24T08:40:00", note="渲染失败")

    expire([tried], datetime(2026, 9, 24, 9, 45))

    assert "试过 2 次" in tried.note


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------


def _scheduler(tmp_path: Path, **overrides) -> Scheduler:
    settings = {"retry_minutes": 45, "max_attempts": 3, "seed": 5}
    settings.update(overrides)
    return Scheduler(tmp_path / ".daemon_state.json",
                     windows=parse_windows(["07:30-09:30", "18:30-21:00"]), **settings)


def test_scheduler_round_trips_plans_through_disk(tmp_path: Path) -> None:
    today = date(2026, 9, 24)
    first = _scheduler(tmp_path)
    first.ensure_plans(today)
    expected = [p.planned_at for p in first.plans_for(today)]
    first.save()

    second = _scheduler(tmp_path)
    assert [p.planned_at for p in second.plans_for(today)] == expected


def test_scheduler_claim_and_finish_walks_the_retry_ladder(tmp_path: Path) -> None:
    today = date(2026, 9, 24)
    scheduler = _scheduler(tmp_path)
    scheduler.ensure_plans(today)
    plan = scheduler.plans_for(today)[0]
    now = datetime(2026, 9, 24, 8, 0)

    scheduler.claim(plan, now)
    assert plan.status == RUNNING and plan.attempts == 1
    assert scheduler.last_started_at == now

    # 第一次失败 → 退回待发，还能再试
    scheduler.finish(plan, now + timedelta(minutes=20), exit_code=1, note="渲染挂了")
    assert plan.status == PENDING and plan.attempts == 1
    assert scheduler.state["stats"]["failures"] == 0

    scheduler.claim(plan, now + timedelta(hours=1))
    scheduler.finish(plan, now + timedelta(hours=1, minutes=20), exit_code=1, note="又挂了")
    scheduler.claim(plan, now + timedelta(hours=2))
    scheduler.finish(plan, now + timedelta(hours=2, minutes=20), exit_code=1, note="还是挂了")

    assert plan.status == FAILED and plan.attempts == 3
    assert scheduler.state["stats"]["failures"] == 1


def test_scheduler_records_a_successful_run_and_its_topic(tmp_path: Path) -> None:
    today = date(2026, 9, 24)
    scheduler = _scheduler(tmp_path)
    scheduler.ensure_plans(today)
    plan = scheduler.plans_for(today)[0]

    scheduler.claim(plan, datetime(2026, 9, 24, 8, 0))
    scheduler.finish(plan, datetime(2026, 9, 24, 8, 20), exit_code=0,
                     topic="拓普集团(601689)")

    assert plan.status == DONE and plan.topic == "拓普集团(601689)"
    assert scheduler.state["stats"]["runs"] == 1


def test_scheduler_marks_yesterdays_leftovers_missed(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    scheduler.ensure_plans(date(2026, 9, 23))
    scheduler.save()

    scheduler.decide(datetime(2026, 9, 24, 6, 0))

    for plan in scheduler.plans_for(date(2026, 9, 23)):
        assert plan.status == MISSED
    assert scheduler.state["stats"]["missed"] == 2


def test_next_wake_at_points_at_the_next_slot(tmp_path: Path) -> None:
    """守护进程靠这个数睡觉 —— 空闲时不该一分钟一次地问「到点了吗」。"""
    scheduler = _scheduler(tmp_path)
    now = datetime(2026, 9, 24, 6, 0)
    scheduler.refresh(now)
    morning, evening = scheduler.plans_for(date(2026, 9, 24))

    assert scheduler.next_wake_at(now) == morning.when

    scheduler.claim(morning, morning.when)
    scheduler.finish(morning, morning.when + timedelta(minutes=20), exit_code=0)

    # 早上那条发完了，下一次睁眼就该是晚上的随机点（而不是「马上」）。
    assert scheduler.next_wake_at(datetime(2026, 9, 24, 9, 0)) == evening.when


def test_next_wake_at_can_look_past_today(tmp_path: Path) -> None:
    """晚上发完之后，下一觉应该睡到明天早上的随机点 —— 一睡一整夜。"""
    scheduler = _scheduler(tmp_path)
    now = datetime(2026, 9, 24, 6, 0)
    scheduler.refresh(now)
    for plan in scheduler.plans_for(date(2026, 9, 24)):
        scheduler.claim(plan, plan.when)
        scheduler.finish(plan, plan.when + timedelta(minutes=20), exit_code=0)

    wake = scheduler.next_wake_at(datetime(2026, 9, 24, 22, 0))

    assert wake == scheduler.plans_for(date(2026, 9, 25))[0].when


def test_a_query_only_pass_never_touches_the_disk(tmp_path: Path) -> None:
    """闲置的守护进程不该有任何磁盘活动 —— 这就是「不消耗资源」的落点。"""
    state_file = tmp_path / ".daemon_state.json"
    scheduler = _scheduler(tmp_path)
    scheduler.refresh(datetime(2026, 9, 24, 6, 0))
    scheduler.save()
    stamp = state_file.stat().st_mtime_ns

    for _ in range(50):                       # 到点前的例行「睁眼」
        scheduler.decide(datetime(2026, 9, 24, 6, 30))
        scheduler.next_wake_at(datetime(2026, 9, 24, 6, 30))
        scheduler.save()

    assert scheduler.dirty is False
    assert state_file.stat().st_mtime_ns == stamp


def test_a_status_pass_settles_the_past_so_it_is_not_reported_as_pending(tmp_path: Path) -> None:
    """过了窗口的档位在 --daemon-status 里必须显示「漏了」，不能还挂着「待发」。"""
    scheduler = _scheduler(tmp_path)
    scheduler.ensure_plans(date(2026, 9, 24))
    scheduler.refresh(datetime(2026, 9, 24, 16, 0))

    morning = scheduler.plans_for(date(2026, 9, 24))[0]
    assert morning.status == MISSED


def test_prune_forgets_old_days(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    scheduler.ensure_plans(date(2026, 9, 1), days=1)
    scheduler.ensure_plans(date(2026, 9, 24), days=1)
    scheduler.prune(date(2026, 9, 24))
    assert "2026-09-01" not in scheduler.state["plans"]
    assert "2026-09-24" in scheduler.state["plans"]


def test_status_lines_show_today_and_the_running_totals(tmp_path: Path) -> None:
    today = date(2026, 9, 24)
    scheduler = _scheduler(tmp_path)
    scheduler.ensure_plans(today)
    scheduler.save()

    lines = scheduler.status_lines(datetime(2026, 9, 24, 6, 0))

    assert any(line.startswith("今天 07:30-09:30") for line in lines)
    assert any(line.startswith("明天 18:30-21:00") for line in lines)
    assert any(line.startswith("累计:") for line in lines)


def test_status_stays_scannable_after_a_failure_with_a_long_tail(tmp_path: Path) -> None:
    """子进程末尾输出有十几行；直接塞进 status 会把一行报告撑成一片。"""
    scheduler = _scheduler(tmp_path, max_attempts=2)
    scheduler.ensure_plans(date(2026, 9, 24))
    plan = scheduler.plans_for(date(2026, 9, 24))[0]
    tail = "\n".join(f"第 {index} 行输出内容不算短" for index in range(12))

    scheduler.claim(plan, datetime(2026, 9, 24, 8, 0))
    scheduler.finish(plan, datetime(2026, 9, 24, 8, 20), exit_code=1, note=tail)
    scheduler.claim(plan, datetime(2026, 9, 24, 8, 45))
    scheduler.finish(plan, datetime(2026, 9, 24, 9, 0), exit_code=1, note=tail)

    lines = scheduler.status_lines(datetime(2026, 9, 24, 10, 0))

    failed = [line for line in lines if "[放弃]" in line]
    assert failed, "失败的档位应该在 status 里"
    assert "\n" not in failed[0]
    assert len(failed[0]) < 160


def test_empty_windows_means_nothing_is_ever_planned(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / ".daemon_state.json", windows=[],
                          retry_minutes=45, max_attempts=3)
    scheduler.ensure_plans(date(2026, 9, 24))
    assert scheduler.plans_for(date(2026, 9, 24)) == []
