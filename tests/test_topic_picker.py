"""选题轮转测试：解析 tongstock 热门榜、挑榜首未发过的、记住结果。

定时任务不知道代码，选题错了会发错票，所以这层的行为要钉住：
榜单解析要能容忍前置日志行，已发布/连续失败的票必须跳过，失败一次还能重试。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from stocktalk.modules.topic_picker import HotTopic, TopicError, TopicPicker


@dataclass
class FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


RANKING = {
    "date": "2026-09-23",
    "status": "ok",
    "items": [
        {"code": "000910", "name": "大亚圣象", "mentions": 12, "hotScore": 25},
        {"code": "605058", "name": "澳弘电子", "mentions": 4, "hotScore": 75},
        {"code": "600675", "name": "中华企业", "mentions": 2, "hotScore": 100},
    ],
}


def _picker(tmp_path: Path, runner: Any, **topics_cfg: Any) -> TopicPicker:
    config = {
        "output": {"dir": str(tmp_path)},
        "topics": {"timeout_seconds": 5, **topics_cfg},
    }
    return TopicPicker(config, runner=runner)


def _ranking_runner(payload: dict[str, Any] | None = None, prefix: str = ""):
    body = json.dumps(payload if payload is not None else RANKING, ensure_ascii=False)
    return lambda command, cwd: FakeCompleted(stdout=f"{prefix}{body}")


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def test_topics_parses_ranking_and_tolerates_leading_log_lines(tmp_path: Path) -> None:
    picker = _picker(tmp_path, _ranking_runner(prefix="同步本地行情数据...\nconnected\n"))

    topics = picker.topics()

    assert [t.code for t in topics] == ["000910", "605058", "600675"]
    assert topics[0].name == "大亚圣象"
    assert topics[0].mentions == 12
    assert topics[0].rank == 1
    assert "大亚圣象" in topics[0].describe() and "000910" in topics[0].describe()


def test_topics_passes_the_top_n_flag(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def runner(command: list[str], cwd: Path) -> FakeCompleted:
        seen.append(list(command))
        return FakeCompleted(stdout=json.dumps(RANKING, ensure_ascii=False))

    _picker(tmp_path, runner).topics(top=5)

    assert seen[0][:2] == ["tongstock", "topic"]
    assert "--json" in seen[0]
    assert seen[0][seen[0].index("-n") + 1] == "5"


def test_topics_raises_when_ranking_is_empty(tmp_path: Path) -> None:
    picker = _picker(tmp_path, _ranking_runner({"status": "empty", "items": []}))

    with pytest.raises(TopicError, match="榜单为空"):
        picker.topics()


def test_topics_raises_when_output_is_not_json(tmp_path: Path) -> None:
    picker = _picker(tmp_path, lambda command, cwd: FakeCompleted(stdout="网络不可用"))

    with pytest.raises(TopicError, match="JSON"):
        picker.topics()


def test_topics_reports_a_missing_executable(tmp_path: Path) -> None:
    def runner(command: list[str], cwd: Path) -> FakeCompleted:
        raise FileNotFoundError("tongstock")

    with pytest.raises(TopicError, match="tongstock"):
        _picker(tmp_path, runner).topics()


def test_topics_reports_a_timeout(tmp_path: Path) -> None:
    def runner(command: list[str], cwd: Path) -> FakeCompleted:
        raise subprocess.TimeoutExpired(cmd=list(command), timeout=5)

    with pytest.raises(TopicError, match="超时"):
        _picker(tmp_path, runner).topics()


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def _topics() -> list[HotTopic]:
    return [HotTopic(rank=i, code=c, name=n, mentions=m, hot_score=s)
            for i, (c, n, m, s) in enumerate(
                [("000910", "大亚圣象", 12, 25), ("605058", "澳弘电子", 4, 75), ("600675", "中华企业", 2, 100)],
                start=1)]


def test_select_prefers_the_first_unused_topic(tmp_path: Path) -> None:
    picker = _picker(tmp_path, _ranking_runner())

    assert picker.select(_topics()).code == "000910"

    picker.record(_topics()[0], ok=True)
    assert picker.select(_topics()).code == "605058"


def test_a_failed_topic_can_be_retried_but_not_forever(tmp_path: Path) -> None:
    picker = _picker(tmp_path, _ranking_runner())

    picker.record(_topics()[0], ok=False)
    record = picker.ledger()["000910"]
    assert record["published"] is False and record["runs"] == 1
    assert picker.select(_topics()).code == "000910"      # 第一次失败还给它机会

    picker.record(_topics()[0], ok=False)
    assert picker.ledger()["000910"]["runs"] == 2
    assert picker.select(_topics()).code == "605058"      # 两次失败就让位，别卡在同一只票


def test_select_returns_none_when_the_whole_ranking_is_spent(tmp_path: Path) -> None:
    picker = _picker(tmp_path, _ranking_runner())
    for topic in _topics():
        picker.record(topic, ok=True)

    assert picker.select(_topics()) is None


def test_ledger_keeps_a_published_topic_from_coming_back(tmp_path: Path) -> None:
    picker = _picker(tmp_path, _ranking_runner())
    picker.record(_topics()[0], ok=True)

    payload = json.loads(picker.rotation_path.read_text(encoding="utf-8"))

    assert payload["topics"]["000910"]["published"] is True
    assert payload["updated_at"]


def test_ledger_prunes_entries_older_than_the_retention_window(tmp_path: Path) -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    picker = _picker(tmp_path, _ranking_runner(), clock=lambda: now)
    picker.rotation_path.write_text(json.dumps({
        "topics": {
            "000001": {"name": "老票", "first_seen": "2026-01-01T09:00:00+08:00",
                       "last_run": "2026-01-01T09:00:00+08:00", "runs": 1, "published": True},
            "000910": {"name": "大亚圣象", "first_seen": "2026-09-22T09:00:00+08:00",
                       "last_run": "2026-09-22T09:00:00+08:00", "runs": 1, "published": True},
        }
    }, ensure_ascii=False), encoding="utf-8")

    picker.record(_topics()[1], ok=True)

    ledger = picker.ledger()
    assert "000001" not in ledger          # 一个月前的「已发过」没有参考价值
    assert {"000910", "605058"} <= set(ledger)


def test_broken_ledger_file_does_not_stop_a_run(tmp_path: Path) -> None:
    picker = _picker(tmp_path, _ranking_runner())
    picker.rotation_path.write_text("{ 这不是 JSON", encoding="utf-8")

    assert picker.ledger() == {}
    assert picker.select(_topics()).code == "000910"
