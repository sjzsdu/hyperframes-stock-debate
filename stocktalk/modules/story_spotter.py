"""数据故事发现器：从多年财务池里挑出这家公司**最有故事**的角度。

用户诉求（2026-09-29）：「把可用的池子做大，针对个股的情况挑选出来讨论，
这样每一个视频都有个性化都有特点」。这里只做**显著性排序**、不做任何判断：
排序依据全部来自数据本身（变化幅度、方向翻转、结构位移），输出交给
dialogue prompt 当「本期重点」——讲什么仍由骨架与模型决定，但素材不再每期
都一样。没有数据（新股、缺块）就返回空表，绝不为了个性化编一个故事。
"""
from __future__ import annotations

from typing import Any, Mapping

from stocktalk.modules.hyperframes_builder import short_business_label

# 科目变动里值得讲的名字（与图板 balance_sheet 的白名单一致）。
_WATCH_ITEMS: tuple[str, ...] = ("货币资金", "存货", "应收款项", "合同负债", "短期借款",
                                 "长期借款", "固定资产", "在建工程", "研发投入", "长期股权投资")


def _annual(metrics: Mapping[str, Any], name: str,
            periods: list[str]) -> list[tuple[str, float]]:
    """某个年度指标的有效点；长度与报告期对不上就整体放弃，不做部分拼接。"""
    values = metrics.get(name) if isinstance(metrics, Mapping) else None
    if not isinstance(values, list) or len(values) != len(periods):
        return []
    return [(str(periods[index]), float(value)) for index, value in enumerate(values)
            if isinstance(value, (int, float)) and not isinstance(value, bool)]


def spot_stories(stock_data: Mapping[str, Any], limit: int = 5) -> list[dict[str, str]]:
    """按显著性挑出本期值得讨论的角度，返回 ``[{kind, headline, detail}]``。"""
    f10 = stock_data.get("f10") if isinstance(stock_data.get("f10"), Mapping) else {}
    profile = f10.get("profile") if isinstance(f10.get("profile"), Mapping) else {}
    history = profile.get("财务历史") if isinstance(profile.get("财务历史"), Mapping) else {}
    annual = history.get("年度") if isinstance(history.get("年度"), Mapping) else {}
    periods = [str(p) for p in (annual.get("报告期") or [])]
    metrics = annual.get("指标") if isinstance(annual.get("指标"), Mapping) else {}
    stories: list[tuple[float, dict[str, str]]] = []

    def add(kind: str, headline: str, detail: str, score: float) -> None:
        stories.append((score, {"kind": kind, "headline": headline, "detail": detail}))

    # 1. 业绩趋势：盈亏翻转最值得讲（首尾都是负数也可能中间翻过盈），其次是幅度。
    profit = _annual(metrics, "归母净利润(亿元)", periods)
    if len(profit) >= 3:
        first, last = profit[0][1], profit[-1][1]
        swing = last - first
        signs = [1 if value > 0 else -1 if value < 0 else 0 for _, value in profit]
        flips = sum(1 for a, b in zip(signs, signs[1:]) if a and b and a * b < 0)
        if flips >= 2:
            profit_years = sum(1 for s in signs if s > 0)
            loss_years = sum(1 for s in signs if s < 0)
            add("profit_trend", f"{len(profit)} 年里 {profit_years} 年盈利、{loss_years} 年亏损，"
                                f"{profit[0][0][:4]}年 {first:,.2f} 亿 → {profit[-1][0][:4]}年 {last:,.2f} 亿",
                "、".join(f"{year[:4]}年 {value:,.2f} 亿" for year, value in profit), 2.5)
        elif abs(swing) >= max(0.5, abs(first) * 0.3):
            if first >= 0 >= last:
                verdict = "由盈转亏"
            elif first < 0 <= last:
                verdict = "由亏转盈"
            elif swing < 0:
                verdict = "五年间明显缩水" if first > 0 and last > 0 else "亏损在扩大"
            else:
                verdict = "五年间持续改善" if first < 0 and last < 0 else "稳步增长"
            add("profit_trend", f"归母净利润{verdict}：{profit[0][0][:4]}年 {first:,.2f} 亿 → "
                                f"{profit[-1][0][:4]}年 {last:,.2f} 亿",
                "、".join(f"{year[:4]}年 {value:,.2f} 亿" for year, value in profit),
                min(3.0, abs(swing) / max(0.5, abs(first) + abs(last)) * 3))

    # 2. 毛利率走势：五年摆动超过 2 个百分点才算有故事。
    margin = _annual(metrics, "销售毛利率(%)", periods)
    if len(margin) >= 3:
        first, last = margin[0][1], margin[-1][1]
        if abs(last - first) >= 2:
            peak = max(margin, key=lambda item: item[1])
            add("margin_trend", f"销售毛利率从 {first:.2f}% 走到 {last:.2f}%"
                                f"（{peak[0][:4]}年摸到 {peak[1]:.2f}%）",
                "、".join(f"{year[:4]}年 {value:.2f}%" for year, value in margin),
                min(3.0, abs(last - first) / 2))

    # 3. 股东回报：连续为负或连续下滑都是硬事实。
    roe = _annual(metrics, "净资产收益率(%)", periods)
    if len(roe) >= 3 and all(value < 0 for _, value in roe[-3:]):
        add("roe_trend", f"净资产收益率连续 {min(3, len(roe))} 年为负",
            "、".join(f"{year[:4]}年 {value:.2f}%" for year, value in roe), 2.0)

    # 4. 现金流：经营为负、靠筹资补血，是「钱从哪来」最直接的故事。
    operating = _annual(metrics, "经营活动现金流量净额(亿元)", periods)
    financing = _annual(metrics, "筹资活动现金流量净额(亿元)", periods)
    if operating and operating[-1][1] < 0:
        extra = ""
        if financing and financing[-1][1] > 0:
            extra = f"，同期筹资净流入 {financing[-1][1]:,.2f} 亿在补"
        add("cashflow", f"最新年度经营活动现金流净流出 {abs(operating[-1][1]):,.2f} 亿{extra}",
            "、".join(f"{year[:4]}年 {value:,.2f} 亿" for year, value in operating),
            min(3.0, abs(operating[-1][1]) / 2))

    # 5. 科目异动：|变动率| 最大的两个白名单科目。
    changes = history.get("科目变动") if isinstance(history.get("科目变动"), Mapping) else {}
    rated = [(abs(float(row["变动率(%)"])), row) for row in (changes.get("明细") or [])
             if isinstance(row, Mapping) and row.get("科目") in _WATCH_ITEMS
             and isinstance(row.get("变动率(%)"), (int, float))
             and isinstance(row.get("本期(亿元)"), (int, float))
             and isinstance(row.get("上期(亿元)"), (int, float))]
    for score, row in sorted(rated, reverse=True)[:2]:
        rate = float(row["变动率(%)"])
        if abs(rate) < 15:
            continue
        add("balance", f"{row['科目']}较上期{'增加' if rate > 0 else '减少'} {abs(rate):.1f}%"
                       f"（{float(row['上期(亿元)']):,.2f} 亿 → {float(row['本期(亿元)']):,.2f} 亿）",
            f"报告期 {changes.get('报告期')}", min(3.0, abs(rate) / 15))

    # 6. 收入结构位移：同一产品线两期占比差 ≥ 5 个百分点。
    composition = profile.get("主营构成历史")
    if isinstance(composition, list) and len(composition) >= 2:
        newest, older = composition[0], composition[-1]
        if isinstance(newest, Mapping) and isinstance(older, Mapping):

            def shares(period: Mapping[str, Any]) -> dict[str, float]:
                result: dict[str, float] = {}
                for row in (period.get("明细") or []):
                    if not isinstance(row, Mapping):
                        continue
                    try:
                        result[str(row["项目"])] = float(str(row["收入占比(%)"]).replace("%", ""))
                    except (KeyError, ValueError, TypeError):
                        continue
                return result

            previous = shares(older)
            for row in newest.get("明细") or []:
                if not isinstance(row, Mapping):
                    continue
                name = str(row.get("项目") or "")
                try:
                    current = float(str(row["收入占比(%)"]).replace("%", ""))
                except (KeyError, ValueError, TypeError):
                    continue
                before = previous.get(name)
                if before is not None and abs(current - before) >= 5:
                    label = short_business_label(name)
                    add("structure", f"{label}收入占比从 {before:.1f}% 变为 {current:.1f}%",
                        f"{older.get('报告期')} → {newest.get('报告期')}",
                        min(3.0, abs(current - before) / 5))

    return [story for _, story in sorted(stories, key=lambda item: -item[0])][:limit]
