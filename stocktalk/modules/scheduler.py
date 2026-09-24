"""排期引擎 —— 决定「现在该不该发一条」，以及「下一次该看表是什么时候」。

两条设计线
----------

**时刻要随机。** 每天的两条在 07:30-09:30、18:30-21:00 里各随机取一点，时刻天天
不同。窗口本身是「什么时候发都合理」的范围（早晚高峰），随机只负责让它别每天一样 ——
连续一个月 08:30:00 准时出现，平台的规律性识别几乎不费力。

**错过就认。** 这条是有意的：个人电脑会睡眠会关机，但事后补发并不划算 —— 早班拖到
下午才补出去，那个时刻反而更假，还会把当天的节奏搅乱。所以规则很硬：**只在窗口内
发，窗口一过就记 ``missed``**。可靠性不靠事后追认，靠「窗口有两个小时宽，机器只要
在其中任意一刻醒着就行」。

**不靠轮询。** :meth:`Scheduler.next_wake_at` 直接算出「下一次值得睁眼的时刻」，空闲
时守护进程一觉睡到那儿（详见 :mod:`stocktalk.modules.daemon`）；排期读写在内存里完成，
只有状态真的变了才落盘。所以它闲着的时候既不烧 CPU 也不碰磁盘。

状态全部落在 JSON 里（``.daemon_state.json``）：计划、每次跑的退出码、事件。守护进程
随时可以被杀掉重启，重启后接着算，不会重复发也不会丢档。
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------

# 允许 07:30-09:30 / 7:30~9:30 / 7:30—9:30（中文破折号），分隔符不挑。
_WINDOW_SEPARATOR = re.compile(r"\s*[-~－—–至]\s*")

# 随机取点时在窗口尾部留出的余量。守护进程按计划时刻醒来，但唤醒、结算、起子进程
# 都要花掉几秒，取到窗口最后一分钟等于自找「差一点就过期」。窗口很窄时按四分之一收缩。
_PICK_TAIL_MARGIN_MINUTES = 10


class ScheduleConfigError(ValueError):
    """窗口或时长写错了。启动时就报，不要等到该发的那一刻才发现。"""


@dataclass(frozen=True)
class Window:
    """一天里的一个发布窗口，例如 07:30-09:30。

    窗口的意义：``start`` 到 ``end`` 之间任何一个时刻发布都合理（早晚高峰），
    具体落在哪一点由随机决定，所以「今天 08:12、明天 07:48」看起来只是随手发的。
    """

    start: time
    end: time

    @property
    def label(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"

    def span_minutes(self) -> int:
        return _minutes(self.end) - _minutes(self.start)

    def pick(self, day: date, rng: random.Random) -> datetime:
        """窗口内均匀取一个时刻，尾部留一点余量（见 ``_PICK_TAIL_MARGIN_MINUTES``）。"""
        span = max(0, self.span_minutes())
        margin = min(_PICK_TAIL_MARGIN_MINUTES, span // 4)
        return datetime.combine(day, self.start) + timedelta(minutes=rng.randint(0, span - margin))

    def ends_on(self, day: date) -> datetime:
        return datetime.combine(day, self.end)


def parse_window(raw: str | Window) -> Window:
    """``"07:30-09:30"`` → :class:`Window`。已经解析过的原样返回。"""
    if isinstance(raw, Window):
        return raw
    text = str(raw or "").strip()
    if not text:
        raise ScheduleConfigError("发布窗口不能为空")
    parts = _WINDOW_SEPARATOR.split(text)
    if len(parts) != 2:
        raise ScheduleConfigError(f"看不懂的发布窗口：{raw!r}（写成 07:30-09:30）")
    start, end = (_parse_clock(token, raw) for token in parts)
    if start == end:
        raise ScheduleConfigError(f"发布窗口起止相同：{raw!r}（窗口至少要有 1 分钟）")
    if end < start:
        raise ScheduleConfigError(
            f"发布窗口跨了午夜：{raw!r}。跨零点请写成两个窗口（如 22:00-23:30 与 00:00-01:30）"
        )
    return Window(start=start, end=end)


def parse_windows(raw_list: Any) -> list[Window]:
    """解析窗口列表，顺带挡住「两个窗口重叠」这种会让两条撞在一起的手误。"""
    windows = [parse_window(item) for item in (raw_list or ())]
    ordered = sorted(windows, key=lambda item: _minutes(item.start))
    for earlier, later in zip(ordered, ordered[1:]):
        if later.start <= earlier.end:
            raise ScheduleConfigError(
                f"发布窗口重叠：{earlier.label} 与 {later.label}（重叠会让同一天的两条撞在一起）"
            )
    return ordered


def _parse_clock(token: str, origin: Any) -> time:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", token.strip())
    if not match:
        raise ScheduleConfigError(f"看不懂的发布窗口：{origin!r}（写成 07:30-09:30）")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleConfigError(f"发布窗口的时间不合法：{origin!r}")
    return time(hour=hour, minute=minute)


def _minutes(moment: time) -> int:
    return moment.hour * 60 + moment.minute


# ---------------------------------------------------------------------------
# 计划项
# ---------------------------------------------------------------------------

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
MISSED = "missed"

STATUS_LABELS = {
    PENDING: "待发",
    RUNNING: "跑着",
    DONE: "已发",
    FAILED: "放弃",
    MISSED: "漏了",
}


@dataclass
class Plan:
    """一条待跑的发布任务。``planned_at`` 是那天随机抽到的那个时刻。"""

    day: str
    window: str
    planned_at: str
    status: str = PENDING
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    topic: str = ""
    note: str = ""

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.planned_at)

    @property
    def window_end(self) -> datetime:
        """所属窗口的结束时刻；解析不出来时退回 ``planned_at``（宁可早跑，别永远不跑）。"""
        try:
            return parse_window(self.window).ends_on(date.fromisoformat(self.day))
        except (ScheduleConfigError, ValueError):
            return self.when

    def retry_ready_at(self, retry_minutes: float) -> datetime:
        """失败过的档位下一次可以再试的时刻（没失败过就是 ``planned_at``）。"""
        if not self.attempts or retry_minutes <= 0:
            return self.when
        last = self.finished_at or self.started_at
        if not last:
            return self.when
        return max(self.when, datetime.fromisoformat(last) + timedelta(minutes=retry_minutes))

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        known = {key: data.get(key) for key in cls.__dataclass_fields__ if key in data}
        return cls(**known)


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    """这一 tick 的结论。``plan`` 不为空时表示「现在就跑它」。"""

    plan: Plan | None = None
    next_at: datetime | None = None
    reason: str = ""


def decide(plans: list[Plan], now: datetime, *, retry_minutes: float) -> Decision:
    """挑出这一 tick 该跑的那条计划。

    调用方（:meth:`Scheduler.decide`）已经先把过了窗口的档位标成 ``missed``，所以这里
    剩下的 pending 都还在自己的窗口里 —— 到点就跑，不必区分「准时」和「窗口内偏晚」，
    那个差别只对日志好看，对行为没有影响。

    唯一的节流是 ``retry_minutes``：刚失败的档位在退避窗口里不重试，否则一次网络抖动
    会被连着重试到耗尽次数。
    """
    pending = [plan for plan in sorted(plans, key=lambda item: item.planned_at)
               if plan.status == PENDING]

    soonest: datetime | None = None
    for plan in pending:
        ready_at = plan.retry_ready_at(retry_minutes)
        if ready_at > now:
            soonest = ready_at if soonest is None else min(soonest, ready_at)
            continue
        return Decision(plan=plan, next_at=now, reason="到点")

    return Decision(next_at=soonest, reason="没到点")


def expire(plans: list[Plan], now: datetime) -> list[Plan]:
    """把窗口已经过去的 pending 标成 missed。返回这次刚被标掉的那些。

    窗口就是硬边界：过了就不再发。补一条几小时前的片子，那个时间点反而更假 ——
    而「窗口内随便哪一刻醒着都行」已经给了两小时的容错。

    已经试过的档位要带上次数：不然会分不清「没轮到」和「试了两次都没成」。
    """
    dropped: list[Plan] = []
    for plan in plans:
        if plan.status != PENDING:
            continue
        if now > plan.window_end:
            plan.status = MISSED
            tried = f"（试过 {plan.attempts} 次）" if plan.attempts else ""
            plan.note = f"错过 {plan.window}{tried}，窗口已过"
            dropped.append(plan)
    return dropped


# ---------------------------------------------------------------------------
# 计划生成
# ---------------------------------------------------------------------------

def build_plans(
    day: date,
    windows: list[Window],
    rng: random.Random,
    existing: list[Plan] | None = None,
) -> list[Plan]:
    """为 ``day`` 生成计划；同一天重新调用不会重掷骰子。

    幂等不是洁癖：守护进程会被 launchd 反复重启，每次重启都重掷的话，发布时刻会漂移，
    而且已经跑过的档位可能被当成新的再跑一遍。
    """
    existing = existing or []
    plans: list[Plan] = []
    for window in windows:
        prior = next((item for item in existing if item.window == window.label), None)
        if prior is None:
            moment = window.pick(day, rng)
            prior = Plan(day=day.isoformat(), window=window.label,
                         planned_at=moment.isoformat(timespec="seconds"))
        plans.append(prior)
    return plans


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------

STATE_VERSION = 1
KEEP_DAYS = 14
KEEP_EVENTS = 60
PLAN_AHEAD_DAYS = 3


class Scheduler:
    """计划 + 状态的读写门面。守护进程只通过它碰 ``.daemon_state.json``。

    状态变更走**内存里的脏标记**：查询类操作（看表、算下一次）绝不会触发磁盘写。
    闲置一整夜的守护进程因此一次盘都不碰 —— 这正是「不消耗资源」的落点。
    """

    def __init__(self, state_path: Path, *, windows: list[Window], retry_minutes: float,
                 max_attempts: int = 3, seed: int | None = None) -> None:
        self.state_path = Path(state_path)
        self.windows = windows
        self.retry_minutes = float(retry_minutes)
        self.max_attempts = max(1, int(max_attempts))
        self._rng = random.Random(seed)
        self._dirty = False
        # 最近一次结算里被判过期的档位。守护进程用它决定要不要报警 —— 错过档位不该
        # 一声不吭，那正是「以为在跑其实早停了」的来源。
        self.last_dropped: list[Plan] = []
        self.state = self._load()

    # -- 存取 -------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("version", STATE_VERSION)
        data.setdefault("plans", {})
        data.setdefault("events", [])
        data.setdefault("last_started_at", None)
        data.setdefault("last_finished_at", None)
        data.setdefault("last_result", None)
        data.setdefault("stats", {"runs": 0, "failures": 0, "missed": 0})
        return data

    @property
    def dirty(self) -> bool:
        return self._dirty

    def save(self, *, force: bool = False) -> bool:
        """落盘。**没有变更就什么都不做** —— 返回是否真的写了。"""
        if not self._dirty and not force:
            return False
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：守护进程随时可能被杀，半截 JSON 会让下次启动丢掉全部排期。
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)
        self._dirty = False
        return True

    @property
    def last_started_at(self) -> datetime | None:
        return _maybe_dt(self.state.get("last_started_at"))

    def record_event(self, kind: str, detail: str, now: datetime) -> None:
        events = self.state.setdefault("events", [])
        events.append({"at": now.isoformat(timespec="seconds"), "kind": kind, "detail": detail})
        del events[:-KEEP_EVENTS]
        self._dirty = True

    # -- 计划 -------------------------------------------------------------

    def plans_for(self, day: date) -> list[Plan]:
        raw = self.state.get("plans", {}).get(day.isoformat(), [])
        return [Plan.from_dict(item) for item in raw if isinstance(item, dict)]

    def _store(self, day: date, plans: list[Plan]) -> None:
        self.state.setdefault("plans", {})[day.isoformat()] = [item.to_dict() for item in plans]

    def ensure_plans(self, today: date, *, days: int = PLAN_AHEAD_DAYS) -> None:
        """为今天起 ``days`` 天准备计划。窗口为空时什么也不做（等于停用）。"""
        if not self.windows:
            return
        for offset in range(days):
            day = today + timedelta(days=offset)
            if self.plans_for(day):
                continue
            self._store(day, build_plans(day, self.windows, self._rng))
            self._dirty = True

    def refresh(self, now: datetime) -> None:
        """准备未来 + 结算历史。只想看状态、不想认领任务时也用它。"""
        self.ensure_plans(now.date())
        self.last_dropped = self._settle_history(now)

    def decide(self, now: datetime) -> Decision:
        """结算历史 → 准备未来 → 给出这一 tick 该跑的那条。

        被结算掉的过期档位留在 :attr:`last_dropped` 里，调用方拿去报警。
        """
        self.refresh(now)
        return decide(self.plans_for(now.date()), now, retry_minutes=self.retry_minutes)

    def next_wake_at(self, now: datetime) -> datetime | None:
        """下一次「值得睁眼」的时刻；未来几天都没有计划时返回 ``None``。

        守护进程拿它当睡眠时长 —— 空闲时不再一分钟一次地轮询，而是一觉睡到下一个
        发布时刻（或重试时刻）。返回 ``None`` 时调用方用自己的兜底上限。
        """
        moments: list[datetime] = []
        for key in sorted(self.state.get("plans", {})):
            day = _safe_date(key)
            if day is None or day < now.date():
                continue
            for plan in self.plans_for(day):
                if plan.status == PENDING:
                    moments.append(plan.retry_ready_at(self.retry_minutes))
        future = [moment for moment in moments if moment > now]
        return min(future) if future else None

    def _settle_history(self, now: datetime) -> list[Plan]:
        """把所有过期档位标掉：今天的按窗口算，更早的日子直接认过期。"""
        dropped: list[Plan] = []
        for key in sorted(self.state.get("plans", {})):
            day = _safe_date(key)
            if day is None or day > now.date():
                continue
            plans = self.plans_for(day)
            if day < now.date():
                changed = []
                for plan in plans:
                    if plan.status == PENDING:
                        plan.status = MISSED
                        plan.note = f"{day.isoformat()} 已过，未发"
                        changed.append(plan)
            else:
                changed = expire(plans, now)
            if changed:
                self._store(day, plans)
            dropped.extend(changed)
        if dropped:
            self._dirty = True
            self.state["stats"]["missed"] = self.state["stats"].get("missed", 0) + len(dropped)
            for plan in dropped:
                self.record_event("missed", f"{plan.day} {plan.window} 的档位未发（{plan.note}）", now)
        return dropped

    def claim(self, plan: Plan, now: datetime) -> None:
        """标记「开始跑了」。先落盘再启动子进程 —— 崩在启动瞬间也要留下痕迹。"""
        plan.status = RUNNING
        plan.started_at = now.isoformat(timespec="seconds")
        plan.attempts += 1
        self.state["last_started_at"] = plan.started_at
        self._replace(plan)
        self._dirty = True
        self.save()

    def finish(self, plan: Plan, now: datetime, *, exit_code: int, note: str = "",
               topic: str = "", result: Mapping[str, Any] | None = None) -> None:
        plan.exit_code = exit_code
        plan.finished_at = now.isoformat(timespec="seconds")
        plan.note = note
        if topic:
            plan.topic = topic
        if exit_code == 0:
            plan.status = DONE
            self.state["stats"]["runs"] = self.state["stats"].get("runs", 0) + 1
        elif plan.attempts >= self.max_attempts:
            plan.status = FAILED
            self.state["stats"]["failures"] = self.state["stats"].get("failures", 0) + 1
        else:
            plan.status = PENDING  # 退回待发，retry_minutes 后由 decide 放行
        self.state["last_finished_at"] = plan.finished_at
        # 上一档的结论留着给 --daemon-status 和通知用：退出码说明不了「哪几个平台成了」，
        # 而「上一次到底发出去没有」是看状态的人唯一的真问题。
        if result is not None:
            self.state["last_result"] = dict(result)
        self._replace(plan)
        self._dirty = True
        self.save()

    def note_heartbeat(self, now: datetime) -> None:
        """心跳时间戳（纯记账，落盘交给调用方的下一次 :meth:`save`）。"""
        self.state["last_heartbeat"] = now.isoformat(timespec="seconds")
        self._dirty = True

    def _replace(self, plan: Plan) -> None:
        day = date.fromisoformat(plan.day)
        plans = self.plans_for(day)
        for index, item in enumerate(plans):
            if item.planned_at == plan.planned_at:
                plans[index] = plan
                break
        else:
            plans.append(plan)
        self._store(day, plans)

    def prune(self, today: date) -> None:
        plans = self.state.get("plans", {})
        cutoff = today - timedelta(days=KEEP_DAYS)
        for key in [key for key in plans if (_safe_date(key) or today) < cutoff]:
            del plans[key]
            self._dirty = True

    # -- 汇报 -------------------------------------------------------------

    def status_lines(self, now: datetime) -> list[str]:
        lines: list[str] = []
        for offset in range(2):
            day = now.date() + timedelta(days=offset)
            label = "今天" if offset == 0 else "明天"
            for plan in self.plans_for(day):
                lines.append(f"{label} {self._describe(plan)}")
        stats = self.state.get("stats", {})
        lines.append("累计: 成功 {runs} / 放弃 {failures} / 漏掉 {missed}".format(
            runs=stats.get("runs", 0), failures=stats.get("failures", 0),
            missed=stats.get("missed", 0)))
        return lines

    def _describe(self, plan: Plan) -> str:
        state = STATUS_LABELS.get(plan.status, plan.status)
        bits = [f"{plan.window}  计划 {plan.when:%H:%M}", f"[{state}]"]
        if plan.status == DONE and plan.finished_at:
            bits.append(f"{plan.finished_at[11:16]} 完成")
        if plan.topic:
            bits.append(plan.topic)
        if plan.status == PENDING and plan.attempts:
            bits.append(f"已试 {plan.attempts} 次")
        if plan.status == RUNNING and plan.started_at:
            bits.append(f"{plan.started_at[11:16]} 开始")
        if plan.status in (FAILED, MISSED) and plan.note:
            # note 里可能是子进程末尾十几行输出；status 要能一眼扫过，不是日志的副本
            # （完整输出在 daemon.log 里）。
            bits.append(_shorten(plan.note))
        return "  ".join(bits)


def _shorten(text: str, limit: int = 70) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _safe_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _maybe_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
