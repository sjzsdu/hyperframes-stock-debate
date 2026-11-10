# Language: 中文
"""`--republish` 写回 sidecar 时不能抹掉其它平台的发布记录（2026-11-10 事故驱动）。

现象：603259 补发百家号成功后，`output/603259_*.json` 里的 `publish.platforms`
只剩 baijiahao —— 几小时前成功的抖音/B站/快手/视频号凭空消失。
根因：`_record_publish` 用本次 report 整体覆盖 `publish`，而补发只跑失败平台。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stocktalk.cli import _merge_publish_records, _record_publish  # noqa: E402


def _item(platform: str, ok: bool, detail: str = "") -> dict:
    return {"platform": platform, "ok": ok, "attempts": 1,
            "label": platform, "detail": detail, "command": []}


def _report(items: list[dict]) -> dict:
    return {
        "platforms": items,
        "succeeded": [i["platform"] for i in items if i["ok"]],
        "failed": [{"platform": i["platform"], "detail": i["detail"]}
                   for i in items if not i["ok"]],
    }


def test_republish_keeps_the_platforms_it_did_not_touch():
    previous = _report([
        _item("douyin", True), _item("bilibili", True), _item("kuaishou", True),
        _item("tencent", True), _item("baijiahao", False, "intercepts pointer events"),
    ])
    retry = _report([_item("baijiahao", True)])

    merged = _merge_publish_records(previous, retry)

    assert [p["platform"] for p in merged["platforms"]] == [
        "douyin", "bilibili", "kuaishou", "tencent", "baijiahao"]
    assert set(merged["succeeded"]) == {"douyin", "bilibili", "kuaishou", "tencent", "baijiahao"}
    assert merged["failed"] == []


def test_newer_result_wins_for_the_same_platform():
    previous = _report([_item("baijiahao", False, "old error")])
    retry = _report([_item("baijiahao", True, "ok now")])

    merged = _merge_publish_records(previous, retry)

    assert len(merged["platforms"]) == 1
    assert merged["platforms"][0]["ok"] is True
    assert merged["platforms"][0]["detail"] == "ok now"


def test_a_retry_that_fails_again_stays_failed():
    previous = _report([_item("douyin", True), _item("baijiahao", False, "first")])
    retry = _report([_item("baijiahao", False, "second")])

    merged = _merge_publish_records(previous, retry)

    assert merged["succeeded"] == ["douyin"]
    assert merged["failed"] == [{"platform": "baijiahao", "detail": "second"}]


def test_fresh_publish_without_history_is_recorded_as_is():
    retry = _report([_item("douyin", True)])

    for empty in (None, {}, {"platforms": []}):
        assert _merge_publish_records(empty, retry)["platforms"] == retry["platforms"]


def test_record_publish_writes_the_merged_result(tmp_path: Path):
    video = tmp_path / "603259_20261110_162203.mp4"
    video.write_bytes(b"x")
    sidecar = video.with_suffix(".json")
    sidecar.write_text(json.dumps({
        "stock_code": "603259",
        "publish": _report([_item("douyin", True), _item("baijiahao", False, "boom")]),
    }), encoding="utf-8")

    _record_publish(video, _report([_item("baijiahao", True)]), {})

    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["stock_code"] == "603259"
    assert [p["platform"] for p in data["publish"]["platforms"]] == ["douyin", "baijiahao"]
    assert data["publish"]["failed"] == []
