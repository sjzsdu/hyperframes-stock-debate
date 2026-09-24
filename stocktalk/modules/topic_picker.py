"""Pick the day's subject from tongstock's hot-topic ranking.

A scheduled pipeline needs a subject every time it fires, and "what the news is
busiest with" is the honest answer: ``tongstock topic`` aggregates the day's
headlines from 东方财富/财联社/证券时报/21 世纪经济报道/第一财经/新浪财经/腾讯财经
and ranks stocks by how strongly they were covered (只有标题命中或数据源原生关联的
强相关内容才计数), so the top of that list is a defensible editorial choice rather
than a guess.

This module only does the choosing and the remembering:

* take the highest-ranked stock that has **not been published yet**;
* a stock that fails once may be retried, but two failed runs is enough — skip
  it, otherwise a run that always dies on one name (bad data, delisted) pins
  every future slot to that name forever;
* the ledger lives next to the renders (``output/.topic_rotation.json``) because
  it describes what *this pipeline* already covered, not the code.

With two runs a day this walks down the same ranking (榜首 → 榜眼 → …) instead of
repeating one stock, and the ledger prunes itself so it cannot grow forever.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_ROTATION_FILENAME = ".topic_rotation.json"
# 同一条目连续失败到这个次数就跳过：一次失败可能是网络/渲染抖动，两次说明这只票
# 本身有问题（数据缺失、退市、无法成片），继续钉在它上面只会让每个时段都空跑。
MAX_RUNS_PER_TOPIC = 2
# 轮转记忆的保留期：热门榜是当日新闻的产物，一个月前的「已发过」没有意义。
KEEP_DAYS = 30


class TopicError(RuntimeError):
    """Raised when the ranking cannot be fetched or parsed into usable topics."""


@dataclass(frozen=True)
class HotTopic:
    """One entry of the daily ranking."""

    rank: int
    code: str
    name: str
    mentions: int = 0
    hot_score: float = 0.0

    def describe(self) -> str:
        return f"#{self.rank} {self.name}({self.code})｜新闻提及 {self.mentions} 次"


def _now() -> datetime:
    return datetime.now().astimezone()


class TopicPicker:
    """Fetch the ranking, choose the next subject, remember what was used."""

    def __init__(self, config: Mapping[str, Any] | None = None, runner: Callable[..., Any] | None = None,
                 executable: str = "tongstock", clock: Callable[[], datetime] = _now) -> None:
        self.config = dict(config or {})
        publish = self.config.get("publish", {}) or {}
        topics_cfg = self.config.get("topics", {}) or {}
        self.executable = str(topics_cfg.get("executable") or executable)
        self.timeout = float(topics_cfg.get("timeout_seconds", 120))
        self.max_runs = max(1, int(topics_cfg.get("max_runs_per_topic", MAX_RUNS_PER_TOPIC)))
        output_dir = Path((self.config.get("output", {}) or {}).get("dir", "output"))
        rotation_file = str(topics_cfg.get("rotation_file")
                            or publish.get("rotation_file") or DEFAULT_ROTATION_FILENAME)
        self.rotation_path = (Path(rotation_file) if Path(rotation_file).is_absolute()
                              else output_dir / rotation_file)
        self._runner = runner or self._run_subprocess
        self._clock = clock

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------
    def _run_subprocess(self, command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(command), cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=self.timeout)

    def topics(self, date: str | None = None, top: int = 10) -> list[HotTopic]:
        """Today's ranking, best first. Raises TopicError with a usable message."""
        command = [self.executable, "topic", "--json", "-n", str(max(1, top))]
        if date:
            command.insert(2, date)
        try:
            completed = self._runner(command, Path.cwd())
        except FileNotFoundError as exc:
            raise TopicError(f"找不到 {self.executable} 命令（{exc}）。选题依赖 tongstock topic。") from exc
        except subprocess.TimeoutExpired as exc:
            raise TopicError(f"{self.executable} topic 超时（{self.timeout:g}s），今日榜单取不到。") from exc
        except OSError as exc:
            raise TopicError(f"{self.executable} topic 调用失败: {exc}") from exc

        raw = f"{completed.stdout or ''}{completed.stderr or ''}"
        payload = _load_json(raw)
        if payload is None:
            raise TopicError(f"{self.executable} topic 没有返回可解析的 JSON（末尾输出: {raw.strip()[-160:]}）")

        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise TopicError(f"{self.executable} topic 榜单为空（status={payload.get('status')!r}），"
                             f"可能当天还没有抓取到新闻。")
        topics: list[HotTopic] = []
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or "").strip()
            if not code:
                continue
            topics.append(HotTopic(
                rank=int(item.get("rank") or index),
                code=code,
                name=name,
                mentions=int(item.get("mentions") or 0),
                hot_score=float(item.get("hotScore") or 0.0),
            ))
        if not topics:
            raise TopicError(f"{self.executable} topic 榜单里没有可用代码")
        return topics

    # ------------------------------------------------------------------
    # Rotation ledger
    # ------------------------------------------------------------------
    def ledger(self) -> dict[str, Any]:
        try:
            data = json.loads(self.rotation_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        topics = data.get("topics") if isinstance(data, dict) else None
        return topics if isinstance(topics, dict) else {}

    def select(self, topics: Sequence[HotTopic]) -> HotTopic | None:
        """Highest-ranked topic that is neither published nor out of attempts."""
        used = self.ledger()
        for topic in topics:
            record = used.get(topic.code)
            if not isinstance(record, dict):
                return topic
            if record.get("published"):
                continue
            if int(record.get("runs") or 0) >= self.max_runs:
                continue
            return topic
        return None

    def record(self, topic: HotTopic, ok: bool) -> dict[str, Any]:
        """Mark a run: published (stop offering it) or one more failed attempt."""
        used = self.ledger()
        record = used.get(topic.code) if isinstance(used.get(topic.code), dict) else {}
        stamp = self._clock().isoformat(timespec="seconds")
        record = {
            "name": topic.name or record.get("name") or "",
            "first_seen": record.get("first_seen") or stamp,
            "last_run": stamp,
            "runs": int(record.get("runs") or 0) + 1,
            "published": bool(record.get("published")) or ok,
            "rank": topic.rank,
        }
        used[topic.code] = record
        self._write(used)
        return record

    def _write(self, used: Mapping[str, Any]) -> None:
        payload = {"topics": self._prune(used), "updated_at": self._clock().isoformat(timespec="seconds")}
        try:
            self.rotation_path.parent.mkdir(parents=True, exist_ok=True)
            self.rotation_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # 记不住轮转只是可能重复一只票，不该打断这次生成。
            pass

    def _prune(self, used: Mapping[str, Any]) -> dict[str, Any]:
        cutoff = self._clock() - timedelta(days=KEEP_DAYS)
        kept: dict[str, Any] = {}
        for code, record in used.items():
            if not isinstance(record, dict):
                continue
            try:
                seen = datetime.fromisoformat(str(record.get("last_run") or record.get("first_seen")))
            except ValueError:
                kept[code] = record
                continue
            if seen.tzinfo is None:
                seen = seen.astimezone()
            if seen >= cutoff:
                kept[code] = record
        return kept


def _load_json(raw: str) -> dict[str, Any] | None:
    """Parse the first JSON object in the output.

    tongstock prints connection/sync log lines before the payload, so the text is
    not JSON from byte zero (same tolerance as stock_data's parser).
    """
    start = raw.find("{")
    if start < 0:
        return None
    try:
        data = json.loads(raw[start:])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
