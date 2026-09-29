"""Normalized A-share data access backed by the :command:`tongstock` CLI.

The CLI is deliberately kept behind a small adapter: callers get predictable
Python dictionaries while command output, retries, and CLI-version differences
remain here.  No trade recommendation is produced by this module.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence


class StockDataError(RuntimeError):
    """Base error for data retrieval failures."""


class TongstockUnavailableError(StockDataError):
    """Raised when the installed tongstock version cannot serve a dataset."""


class TongstockCommandError(StockDataError):
    """Raised after all attempts to execute a tongstock command fail."""


class StockDataIncompleteError(StockDataError):
    """Raised when a dataset required for company-level content is missing.

    Company fundamentals (主营/行业/管理层) are the whole point of the video —
    without them the dialogue can only paraphrase price action, which is not
    knowledge.  A missing dataset therefore aborts the run instead of quietly
    producing a thinner script: the gap belongs in tongstock, not worked around
    here (2026-09-18).
    """


Runner = Callable[[Sequence[str], float], str]

# Datasets without which the script cannot describe the business itself.
REQUIRED_DATASETS: frozenset[str] = frozenset({"f10", "financials", "board"})


@dataclass(frozen=True)
class StockDataConfig:
    """Runtime policy for :class:`StockDataClient`."""

    executable: str = "tongstock"
    retries: int = 2
    retry_delay_seconds: float = 0.35
    # 20s was too tight: `indicator` triggers a first-time TDX sync that takes
    # ~30s, and every run failed with a timeout that looked like an outage.
    timeout_seconds: float = 90.0
    consistency: str = "allow_stale"
    # F10 blocks that carry the business itself: 公司概况 (主营/行业/管理层),
    # 经营分析 (主营构成与毛利率), 财务分析 (多年指标序列与三表科目),
    # 高管治理 (谁在管), 行业分析 (行业地位与同行), 热点题材 (概念/风格标签).
    #
    # 财务分析 过去被排除（`finance` 已经给了当期数字，而这块是 ~38K 字符的
    # 表格文本，整段进 prompt 只会挤掉别的一切）。2026-09-29 改为**解析后**使用：
    # 它是历年营收/净利/毛利率/ROE/周转率走势与三表科目变动的唯一真源，原文
    # 依然不进 prompt，只进结构化结果（见 :meth:`_parse_finance_analysis`）。
    f10_blocks: tuple[str, ...] = ("公司概况", "经营分析", "财务分析", "高管治理",
                                   "行业分析", "热点题材")
    news_limit: int = 12
    news_days: int = 0
    # Company fundamentals are mandatory by default: a missing F10/finance
    # dataset aborts instead of degrading the script to price chatter.  Flip
    # this off only to unblock a run while tongstock is being fixed.
    require_company_data: bool = True


def normalize_code(code: str | int) -> str:
    """Return a six-digit mainland A-share code or raise :class:`ValueError`."""
    value = str(code).strip().upper()
    value = re.sub(r"^(SH|SZ|BJ)", "", value)
    value = re.sub(r"\.(SH|SZ|BJ)$", "", value)
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("A-share code must be a six-digit code, e.g. '600519'")
    return value


class StockDataClient:
    """Fetch quote, F10, financial, indicator, news and K-line data from tongstock."""

    def __init__(self, config: StockDataConfig | None = None, runner: Runner | None = None) -> None:
        self.config = config or StockDataConfig()
        self._runner = runner or self._run_subprocess

    def get_stock_data(self, code: str | int, *, kline_count: int = 60) -> dict[str, Any]:
        """Fetch every dataset the video needs and return one video-ready payload.

        Company-level datasets (f10 / financials / board) are **required**: the
        point of the video is to explain what this company sells and where it
        sits in its industry, and without them the dialogue degenerates into
        price chatter.  A failure there aborts the run with
        :class:`StockDataIncompleteError` instead of being parked in
        ``unavailable`` — the gap has to be fixed in tongstock, not absorbed
        here (2026-09-18).  K-line and news are optional: a thin day still
        yields an honest, shorter script.
        """
        symbol = normalize_code(code)
        result: dict[str, Any] = {
            "code": symbol,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "quote": self.get_quote(symbol),
            "technical": self.get_indicators(symbol, count=kline_count),
            "financials": None,
            "f10": None,
            "board": None,
            "stockinfo": None,
            "news": None,
            "kline": [],
            "unavailable": {},
        }
        for name, operation in (
            ("f10", self.get_f10),
            ("financials", self.get_financials),
            ("board", self.get_board),
            ("stockinfo", self.get_stockinfo),
            ("kline", lambda c: self.get_kline(c, count=kline_count)),
            ("news", self.get_news),
        ):
            try:
                result[name] = operation(symbol)
            except (TongstockUnavailableError, TongstockCommandError, StockDataError) as exc:
                result["unavailable"][name] = str(exc)
                if name in REQUIRED_DATASETS and self.config.require_company_data:
                    raise StockDataIncompleteError(
                        f"tongstock 未能提供「{name}」数据（{exc}）。\n"
                        f"这支视频要靠它讲清公司做什么生意、在行业里的位置，缺了就只能复述行情——那不是知识。\n"
                        f"请在 tongstock 中补齐该能力后重试；临时绕过可把 stock_data.require_company_data 设为 false（不推荐）。"
                    ) from exc
        if not result["kline"]:
            result["kline"] = self._history_to_bars(result["technical"])
        self._enrich_quote(result["quote"], result["technical"])
        if not result["quote"].get("name"):
            # `quote` often returns no name.  Pick the fallback carefully:
            # `block show` echoes a name in its header, but that name comes from
            # the block member files (block_fg.dat/block_gn.dat) and can be
            # years stale — 000066 prints 上证商品 (a rename from ~2017) while
            # every other dataset says 中国长城, and the stale name then leaks
            # into the on-screen title.  F10 公司概况 (公司名称/证券简称) and
            # stockinfo are maintained sources; the codes list is the last
            # resort because `codes list` alone can miss a rename.
            fallback = self._f10_short_name(result.get("f10"))
            if not fallback:
                stockinfo = result.get("stockinfo")
                if isinstance(stockinfo, Mapping):
                    fallback = str(stockinfo.get("name") or "")
            if not fallback:
                board = result.get("board") if isinstance(result.get("board"), Mapping) else {}
                fallback = str(board.get("name") or "")
            result["quote"]["name"] = fallback or self._resolve_name(symbol)
        return result

    @staticmethod
    def _enrich_quote(quote: dict[str, Any], technical: Mapping[str, Any]) -> None:
        """Fill missing quote fields (change_pct) from the latest indicator day."""
        if quote.get("change_pct") is not None:
            return
        history = technical.get("history") if isinstance(technical.get("history"), list) else []
        for item in reversed(history):
            if isinstance(item, Mapping) and isinstance(item.get("price"), Mapping):
                change_pct = StockDataClient._number(item["price"].get("change_pct"))
                if change_pct is not None:
                    quote["change_pct"] = change_pct
                    return

    @staticmethod
    def _history_to_bars(technical: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Derive close-price bars (date/close/volume) from the indicator history.

        The per-day indicator payload embeds each day's close price and volume;
        mapping it onto the bar shape lets the trend and drawdown visuals work
        even when the dedicated K-line endpoint is unavailable.  OHLC fields are
        deliberately NOT synthesized: charts must never fake candle bodies from
        anything but genuine K-line rows.
        """
        history = technical.get("history") if isinstance(technical.get("history"), list) else []
        bars: list[dict[str, Any]] = []
        for item in history:
            if not isinstance(item, Mapping):
                continue
            price = item.get("price") if isinstance(item.get("price"), Mapping) else {}
            close = StockDataClient._number(price.get("current"))
            if close is None:
                continue
            bar: dict[str, Any] = {"date": str(item.get("timestamp") or ""), "close": close}
            change = StockDataClient._number(price.get("change"))
            if change is not None:
                bar["change"] = change
            volume = StockDataClient._number(item.get("volume"))
            if volume is not None:
                bar["volume"] = volume
            bars.append(bar)
        return bars

    def get_quote(self, code: str | int) -> dict[str, Any]:
        symbol = normalize_code(code)
        raw = self._invoke("quote", symbol)
        parsed = self._parse_payload(raw)
        if isinstance(parsed, Mapping):
            return self._normalize_quote(parsed, symbol)
        # Current tongstock releases print a human-readable quote by default.
        return self._parse_text_quote(raw, symbol)

    def get_indicators(self, code: str | int, *, count: int = 60) -> dict[str, Any]:
        if count < 1:
            raise ValueError("count must be positive")
        symbol = normalize_code(code)
        payload = self._expect_mapping(self._invoke("indicator", "--code", symbol, "--count", str(count), "--days", str(count), "--json"))
        history = payload.get("history", [])
        if not isinstance(history, list):
            history = []
        return {
            "summary": self._as_dict(payload.get("summary")),
            "history": [self._as_dict(item) for item in history if isinstance(item, Mapping)],
            "count": self._number(payload.get("count"), default=len(history)),
        }

    def get_financials(self, code: str | int) -> dict[str, Any]:
        """Fetch the finance statement and translate it into readable metrics.

        ``finance`` takes the code as a positional argument (``--code`` is not a
        flag in current releases) and returns TDX pinyin field names.  Amounts
        are in 千元 and share counts in 万股 — verified on 600519, whose
        ZhuYingShouRu 90703264 reads as 907 亿, matching its 2026 H1 revenue.
        Only unit-safe numbers are exposed: raw amounts converted to 亿元 and
        ratios computed from two fields that share a unit.
        """
        symbol = normalize_code(code)
        payload = self._expect_mapping(self._invoke("finance", symbol, "--json"))
        return self._normalize_financials(payload)

    @classmethod
    def _normalize_financials(cls, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Convert TDX finance fields into labelled metrics.

        千元 → 亿元 is 1e-5, 万股 → 亿股 is 1e-4.  毛利率 is deliberately NOT
        computed here: ZhuYingLiRun's 口径 does not match the gross margin shown
        in F10 主营构成, and a wrong margin on screen is worse than none — the
        margin is taken verbatim from 经营分析 instead.
        """
        def yi(key: str) -> float | None:
            value = cls._number(payload.get(key))
            return round(value * 1e-5, 2) if value is not None else None

        def yi_shares(key: str) -> float | None:
            value = cls._number(payload.get(key))
            return round(value * 1e-4, 2) if value is not None else None

        revenue = cls._number(payload.get("ZhuYingShouRu"))
        net_profit = cls._number(payload.get("JingLiRun"))
        margin = round(net_profit / revenue * 100, 2) if revenue and net_profit is not None else None
        updated = str(payload.get("UpdatedDate") or "")
        report_date = f"{updated[:4]}-{updated[4:6]}-{updated[6:8]}" if len(updated) == 8 else ""
        metrics: dict[str, Any] = {}
        for label, value in (("报告期", report_date or None), ("营业收入(亿元)", yi("ZhuYingShouRu")),
                             ("营业利润(亿元)", yi("YingYeLiRun")), ("利润总额(亿元)", yi("LiRunZongHe")),
                             ("净利润(亿元)", yi("JingLiRun")), ("总资产(亿元)", yi("ZongZiChan")),
                             ("净资产(亿元)", yi("JingZiChan")), ("经营现金流(亿元)", yi("JingYingXianJinLiu")),
                             ("存货(亿元)", yi("CunHuo")), ("应收账款(亿元)", yi("YingShouZhangKuan")),
                             ("每股净资产(元)", cls._number(payload.get("MeiGuJingZiChan"))),
                             ("总股本(亿股)", yi_shares("ZongGuBen")),
                             ("流通股本(亿股)", yi_shares("LiuTongGuBen")),
                             ("股东人数(户)", cls._number(payload.get("GuDongRenShu"))),
                             ("净利率(%)", margin)):
            if value not in (None, ""):
                metrics[label] = value
        if not metrics:
            raise StockDataError("tongstock finance returned no usable metrics")
        return metrics

    def get_stockinfo(self, code: str | int) -> dict[str, Any]:
        """Basic info with clean units (市值/净利润 in 亿元) from the local library.

        Optional: it is a convenience view over the same exchange data, used only
        because its units are explicit, unlike the raw TDX finance payload.
        """
        symbol = normalize_code(code)
        payload = self._expect_mapping(self._invoke("stockinfo", symbol, "--json"))
        metrics: dict[str, Any] = {}
        # Only the fields whose unit is verifiable are kept.  stockinfo's
        # jingZiChan/jingLiRun disagree with `finance` by ~10x and by reporting
        # period (澳弘 184 vs 17.99 亿), so net asset/profit come from `finance`
        # alone rather than publishing a number we cannot reconcile.
        for label, key in (("市值(亿元)", "marketCap"), ("总市值(亿元)", "totalMarketCap"),
                           ("换手率(%)", "turnoverRate")):
            value = self._number(payload.get(key))
            if value is not None:
                metrics[label] = round(value, 4)
        if not metrics:
            raise StockDataError("tongstock stockinfo returned no usable metrics")
        return {"code": symbol, "name": str(payload.get("name") or ""), "metrics": metrics}

    def get_f10(self, code: str | int) -> dict[str, Any]:
        """Fetch the F10 catalogue and parse the blocks that describe the business.

        ``company <code> --json`` lists the blocks; ``company-content <code>
        --block <name> --json`` returns one block as **box-drawing table text**
        (``{"block","code","content"}``).  The text is parsed into a structured
        profile (主营/行业/管理层/主营构成/题材) rather than dumped verbatim into
        the prompt: 38K characters of table would crowd out everything else.
        """
        symbol = normalize_code(code)
        directory_payload = self._parse_payload(self._invoke("company", symbol, "--json"))
        sections: dict[str, str] = {}
        unavailable: dict[str, str] = {}
        for block in self.config.f10_blocks:
            try:
                payload = self._parse_payload(
                    self._invoke("company-content", symbol, "--block", block, "--json"))
            except (TongstockUnavailableError, TongstockCommandError, StockDataError) as exc:
                unavailable[block] = str(exc)
                continue
            content = payload.get("content") if isinstance(payload, Mapping) else None
            sections[block] = content.strip() if isinstance(content, str) else ""
        profile = self._build_company_profile(sections)
        # TDX 的 F10 块按字节偏移分发，实测读回的块可能从表格中间开始（证券
        # 简称/所属研究行业这类**块头部**字段会整段消失），块本身却非空——不
        # 记一笔的话，画面少一张排名图无从查起（2026-09-29）。
        for block, marker in (("公司概况", "证券简称"), ("行业分析", "所属研究行业")):
            content = sections.get(block) or ""
            if content and marker not in content:
                unavailable.setdefault(block, f"块内容缺少「{marker}」，该块可能从中段读回")
        if not profile.get("财务历史"):
            # 不阻断：主营业务仍在，视频主体成立；缺的只是多年走势图，图表层
            # 会退回别的图。但必须记一笔，否则「这期怎么没走势图」无从查起。
            unavailable.setdefault("财务分析", "解析不出年度指标序列——本期没有多年走势图")
        if not profile.get("主营业务") and not profile.get("所属行业"):
            raise StockDataError(
                f"F10 分块已取回但解析不出主营业务/所属行业（空分块: "
                f"{', '.join(k for k, v in sections.items() if not v) or '无'}）")
        return {
            "directory": directory_payload if directory_payload is not None else [],
            "sections": sections,
            "profile": profile,
            "unavailable_sections": unavailable,
        }

    @staticmethod
    def _f10_short_name(f10: Any) -> str:
        """The display name from F10: 证券简称 first, then 公司名称.

        证券简称 is what every other screen calls the stock (中国长城, not the
        registered 中国长城科技集团股份有限公司).  Both are read from the
        parsed profile when available and fall back to scanning the raw
        公司概况 table, because block text arrives before parsing succeeds.
        """
        if not isinstance(f10, Mapping):
            return ""
        profile = f10.get("profile") if isinstance(f10.get("profile"), Mapping) else {}
        for key in ("证券简称", "公司名称"):
            value = str(profile.get(key) or "").strip()
            if value:
                return value
        # The profile parser keeps only a fixed field list; scan the raw block
        # text so a rename of the parser cannot silently empty this source.
        overview = f10.get("sections").get("公司概况", "") if isinstance(f10.get("sections"), Mapping) else ""
        if not overview:
            return ""
        rows = StockDataClient._parse_pipe_table(overview)
        for key in ("证券简称", "公司名称"):
            value = str(rows.get(key) or "").strip()
            if value:
                return value
        return ""

    def get_board(self, code: str | int) -> dict[str, Any]:
        """Industry and concept membership for one symbol.

        ``block show -c <code>`` is the only classification tongstock exposes
        (there is no F10 endpoint), and it is real exchange data: 行业板块
        (block_fg.dat) carries attributes such as 行业龙头/非周期股, while 概念板块
        (block_gn.dat) carries the business itself — 白酒概念 for 600519, for
        instance.  That is the difference between a video that recites price
        moves and one that explains what the company actually sells.

        The command also echoes the symbol's name, which is used to fill a quote
        that came back without one.
        """
        symbol = normalize_code(code)
        result: dict[str, Any] = {"code": symbol, "name": "", "industry": [], "concepts": []}
        for key, filename in (("industry", "block_fg.dat"), ("concepts", "block_gn.dat")):
            raw = self._invoke("block", "show", "-c", symbol, "-f", filename)
            name, boards = self._parse_block_output(raw)
            if name and not result["name"]:
                result["name"] = name
            result[key] = boards
        return result

    @staticmethod
    def _parse_block_output(raw: str) -> tuple[str, list[str]]:
        """Parse ``block show`` text into (stock name, board names).

        Output looks like::

            股票: 600519 贵州茅台 所属板块:
            --------------------------------------------------
              白酒概念 (type:2, 49只成分股)
        """
        name = ""
        header = re.search(r"股票:\s*\d{6}\s+(\S+)", raw)
        if header:
            name = header.group(1).strip()
        boards = [match.group(1).strip()
                  for match in re.finditer(r"^\s+(.+?)\s*\(type:", raw, re.MULTILINE)]
        return name, boards

    @classmethod
    def _build_company_profile(cls, sections: Mapping[str, str]) -> dict[str, Any]:
        """Turn raw F10 block text into the facts that make a script worth watching.

        The video promises the viewer they will understand a business, so the
        profile deliberately leads with 主营业务/所属行业/主营构成/管理层 — the
        "what do they sell, who runs it, where does the money come from" layer.
        Everything here is copied out of exchange F10 text: nothing is inferred.
        """
        overview = cls._parse_pipe_table(sections.get("公司概况", ""))
        profile: dict[str, Any] = {}
        for source, label in (("证券简称", "证券简称"), ("公司名称", "公司名称"), ("主营业务", "主营业务"),
                              ("通达信研究行业", "所属行业"), ("证监会行业", "证监会行业"),
                              ("上市日期", "上市日期"), ("董事长", "董事长"), ("法人代表", "法人代表"),
                              ("总经理", "总经理"), ("公司董秘", "董秘")):
            if overview.get(source):
                profile[label] = overview[source]
        if overview.get("经营范围"):
            profile["经营范围"] = overview["经营范围"][:200]
        # 公司简介 is dropped on purpose: TDX fills it with the 股份制改造 history
        # (验资报告/折股比例), which costs prompt budget without explaining the
        # business — 主营业务 and 主营构成 already do that job.
        composition = cls._parse_main_composition(sections.get("经营分析", ""))
        if composition:
            profile["主营构成"] = composition
        # 经营情况评述 is the company describing its own business in prose — the
        # most quotable source for "how does this business actually run".
        review = cls._slice_subsection(sections.get("经营分析", ""), "【5.经营情况评述】")
        review_text = " ".join(re.sub(r"\s+", " ", line).strip() for line in review.splitlines()
                               if line.strip() and "─" not in line and not line.strip().startswith("【"))
        if review_text:
            profile["经营评述"] = review_text[:600]
        executives = cls._parse_executives(sections.get("高管治理", ""))
        if executives:
            profile["管理层"] = executives
        ranking = cls._parse_industry_ranking(sections.get("行业分析", ""),
                                              str(profile.get("公司名称") or ""))
        if ranking:
            profile["行业地位"] = ranking
        themes = cls._parse_themes(sections.get("热点题材", ""))
        if themes:
            profile["题材标签"] = themes
        # 多年财务：有几年数据就能讲几年的事，池子（图表与话题）全靠它做大。
        finance = cls._parse_finance_analysis(sections.get("财务分析", ""))
        if finance:
            profile["财务历史"] = finance
        composition_history = cls._parse_composition_history(sections.get("经营分析", ""))
        if len(composition_history) >= 2:
            profile["主营构成历史"] = composition_history
        return profile

    @staticmethod
    def _parse_pipe_table(text: str) -> dict[str, str]:
        """Parse TDX F10 box tables (``│A│B│C│D│``) into key/value pairs.

        Cells come in pairs; a row whose first cell is blank continues the
        previous value, because long fields like 经营范围/公司简介 wrap across
        lines and would otherwise be lost after the first chunk.
        """
        rows: dict[str, str] = {}
        last_key = ""
        for line in text.splitlines():
            if "│" not in line or "─" in line:
                continue
            cells = [cell.strip() for cell in line.split("│")][1:-1]
            for index in range(0, len(cells) - 1, 2):
                key, value = cells[index], cells[index + 1]
                if key:
                    last_key = key
                    rows[key] = value
                elif last_key:
                    rows[last_key] = (rows.get(last_key, "") + value).strip()
        return rows

    _COMPOSITION_HEADER = re.compile(r"项目名.*营业收入.*毛利率")
    _PERIOD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    # ------------------------------------------------------------------
    # 多期财务序列（F10「财务分析」块）
    # ------------------------------------------------------------------
    # 该块是若干张 box-drawing 表格，同一个指标同时有「年度表」（最近 5 个
    # 12-31 加一列最新中期）和「季度表」（最近 6 个报告期）。所有内容都是 TDX
    # 原文，这里只做搬运与万元→亿元换算，不做任何推导：算不出来的指标宁可不
    # 给，也绝不凑一个数出来（2026-09-29 扩容图表池时的红线）。
    #
    # 块的开头会被 F10 文件切片切掉一部分，导致第一张表**没有列头**。那种表
    # 不靠猜补列头，而是找一张已知列头的表，要求两者存在同名行且数值完全一致
    # （实测「资产负债比率(%)」在两侧都是 64.62/61.76/…），校验通过才认列头，
    # 否则整张表丢弃。
    _FINANCE_METRICS: dict[str, str] = {
        # --- 报告期指标表（行名自带单位，逐行判断换算） ---
        "营业总收(未调整:万)": "营业总收入(亿元)",
        "归母净利(未调整:万)": "归母净利润(亿元)",
        "扣非净利润(万)": "扣非净利润(亿元)",
        "总营收同比增长率(%)": "营收增长率(%)",
        "营业收入增长率": "营收增长率(%)",
        "净利润增长率(%)": "净利润增长率(%)",
        "净利润增长率": "净利润增长率(%)",
        "加权净资产收益率(%)": "净资产收益率(%)",
        "净资产收益率": "净资产收益率(%)",
        "销售毛利率": "销售毛利率(%)",
        "销售净利率": "销售净利率(%)",
        "营业利润率": "营业利润率(%)",
        "总资产净利率": "总资产净利率(%)",
        "资产负债比率(%)": "资产负债率(%)",
        "流动比率": "流动比率",
        "速动比率": "速动比率",
        "产权比率": "产权比率",
        "存货周转率": "存货周转率(次)",
        "应收账款周转率": "应收账款周转率(次)",
        "流动资产周转率": "流动资产周转率(次)",
        "固定资产周转率": "固定资产周转率(次)",
        "总资产周转率": "总资产周转率(次)",
        "营业利润增长率": "营业利润增长率(%)",
        "净资产增长率": "净资产增长率(%)",
        "总资产增长率": "总资产增长率(%)",
        "基本每股收益(元)": "每股收益(元)",
        "每股净资产(元)": "每股净资产(元)",
        "每股经营现金流量(元)": "每股经营现金流(元)",
        "净利润现金含量(%)": "净利润现金含量(%)",
        # --- 资产负债表（整表以万元计，表头已标明） ---
        "货币资金": "货币资金(亿元)",
        "存货": "存货(亿元)",
        "应收票据及应收账款": "应收票据及应收账款(亿元)",
        "合同资产": "合同资产(亿元)",
        "预付款项": "预付款项(亿元)",
        "流动资产合计": "流动资产合计(亿元)",
        "长期股权投资": "长期股权投资(亿元)",
        "固定资产净额": "固定资产净额(亿元)",
        "在建工程": "在建工程(亿元)",
        "无形资产": "无形资产(亿元)",
        "非流动资产合计": "非流动资产合计(亿元)",
        "资产总计": "资产总计(亿元)",
        "应付票据及应付账款": "应付票据及应付账款(亿元)",
        "合同负债": "合同负债(亿元)",
        "流动负债": "流动负债(亿元)",
        "长期借款": "长期借款(亿元)",
        "非流动负债": "非流动负债(亿元)",
        "负债合计": "负债合计(亿元)",
        "资本公积金": "资本公积金(亿元)",
        "未分配利润": "未分配利润(亿元)",
        "归属母公司权益": "归属母公司权益(亿元)",
        "股东权益合计": "股东权益合计(亿元)",
        # --- 利润表 ---
        "营业总收入": "营业总收入(亿元)",
        "营业总成本": "营业总成本(亿元)",
        "营业税金及附加": "营业税金及附加(亿元)",
        "销售费用": "销售费用(亿元)",
        "管理费用": "管理费用(亿元)",
        "财务费用": "财务费用(亿元)",
        "资产减值损失": "资产减值损失(亿元)",
        "投资收益": "投资收益(亿元)",
        "营业利润": "营业利润(亿元)",
        "利润总额": "利润总额(亿元)",
        "所得税": "所得税(亿元)",
        "净利润": "净利润(亿元)",
        "归属母公司净利润": "归母净利润(亿元)",
        # --- 现金流量表 ---
        "经营活动现金流入": "经营活动现金流入(亿元)",
        "经营活动现金流出": "经营活动现金流出(亿元)",
        "经营活动现金流量净额": "经营活动现金流量净额(亿元)",
        "投资活动现金流入": "投资活动现金流入(亿元)",
        "投资活动现金流出": "投资活动现金流出(亿元)",
        "投资活动现金流量净额": "投资活动现金流量净额(亿元)",
        "筹资活动现金流入": "筹资活动现金流入(亿元)",
        "筹资活动现金流出": "筹资活动现金流出(亿元)",
        "筹资活动现金流量净额": "筹资活动现金流量净额(亿元)",
        "现金及等价物净增加额": "现金及等价物净增加额(亿元)",
    }
    # 上屏优先序：最常被讨论的指标在前。图表按精确名取，prompt 按这个顺序取前
    # 若干个，所以把「讲生意」用得最多的指标排在前面。
    _FINANCE_PRIORITY: tuple[str, ...] = (
        "营业总收入(亿元)", "归母净利润(亿元)", "销售毛利率(%)",
        "净资产收益率(%)", "净利润(亿元)", "营收增长率(%)", "净利润增长率(%)",
        "经营活动现金流量净额(亿元)", "资产负债率(%)", "存货(亿元)",
        "应收票据及应收账款(亿元)", "合同负债(亿元)", "资产总计(亿元)",
        "负债合计(亿元)", "归属母公司权益(亿元)", "投资活动现金流量净额(亿元)",
        "筹资活动现金流量净额(亿元)", "未分配利润(亿元)", "营业总成本(亿元)",
        "财务费用(亿元)", "销售费用(亿元)", "管理费用(亿元)", "投资收益(亿元)",
        "存货周转率(次)", "应收账款周转率(次)", "流动比率", "速动比率",
        "每股收益(元)", "每股净资产(元)",
    )
    # 缺列头的表格补列头时必须命中其中之一，避免拿一张不相干的表去套。
    _FINANCE_HEADER_ANCHORS: tuple[str, ...] = ("资产负债比率(%)", "流动比率", "销售毛利率")

    @classmethod
    def _parse_main_composition(cls, text: str, limit: int = 8) -> dict[str, Any]:
        """Extract the newest 主营构成 block (项目/收入/占比/毛利率).

        The block repeats per reporting period, newest first; only the first one
        that actually yields rows is kept.  毛利率 is taken verbatim from here
        rather than computed from the TDX finance payload, whose 口径 differs.
        """
        period = ""
        rows: list[dict[str, str]] = []
        in_table = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("截止日期:"):
                if rows:
                    break  # a later period — we already have the newest one
                period = stripped.split(":", 1)[1].strip()
                in_table = False
                continue
            if not period or "───" in stripped:
                continue
            if cls._COMPOSITION_HEADER.search(stripped):
                in_table = True
                continue
            if not in_table:
                continue
            cells = [cell.strip() for cell in re.split(r"\s{2,}", stripped) if cell.strip()]
            if len(cells) < 3:
                continue
            rows.append({"项目": cells[0], "收入": cells[1], "收入占比(%)": cells[2], "毛利率(%)": cells[-1]})
            if len(rows) >= limit:
                break
        return {"报告期": period, "明细": rows} if rows else {}

    @classmethod
    def _parse_composition_history(cls, text: str, periods: int = 5,
                                   rows: int = 7) -> list[dict[str, Any]]:
        """经营分析的**多期**主营构成，最新一期在前。

        :meth:`_parse_main_composition` 只留最新一期（图板只需要当下结构），
        但「哪块业务这几年在长、哪块的毛利率在掉」只能从多个报告期里看出来，
        这正是让每期视频按个股讲出特点的原料。折行规则与它一致：项目名被 TDX
        按宽度拆到下一行时（``自动化装配与检测生产线及系统集成(`` + ``产品)``），
        续行只有一格，接回上一行而不是当成一条新记录。
        """
        history: list[dict[str, Any]] = []
        period, current, in_table, pending = "", [], False, None

        def flush() -> None:
            if not period or not current:
                return
            picked = [row for row in current if "(产品)" in row["项目"]] or current
            history.append({"报告期": period, "明细": picked[:rows]})

        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("截止日期"):
                flush()
                if len(history) >= periods:
                    return history[:periods]
                period = re.split(r"[:：]", stripped, maxsplit=1)[-1].strip()
                current, in_table, pending = [], False, None
                continue
            if not period or "───" in stripped:
                continue
            if cls._COMPOSITION_HEADER.search(stripped):
                in_table = True
                continue
            if not in_table:
                continue
            cells = [cell.strip() for cell in re.split(r"\s{2,}", stripped) if cell.strip()]
            if len(cells) < 3:
                if cells and len(cells) == 1 and pending is not None:
                    current[pending]["项目"] += cells[0]
                continue
            current.append({"项目": cells[0], "收入": cells[1],
                            "收入占比(%)": cells[2], "毛利率(%)": cells[-1]})
            pending = len(current) - 1
        flush()
        return history[:periods]

    # ------------------------------------------------------------------
    # F10 财务分析块 → 多期序列
    # ------------------------------------------------------------------
    @classmethod
    def _box_tables(cls, text: str) -> list[dict[str, Any]]:
        """把 box-drawing 表格切成 ``{"header": [...] | None, "rows": [[...]]}``。

        连续以 ``│`` 开头的行属于同一张表。列头行靠第二格是报告期来认
        （``YYYY-MM-DD``，因为第一列是行标签）。块开头被切片切掉列头的表也
        照样收下，交给 :meth:`_adopt_header` 交叉校验后再用。
        """
        tables: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for line in text.splitlines():
            stripped = line.strip()
            # `├───┼───┤` 是表内分隔线，`└───┴───┘` 是表尾——都不是新表的开始，
            # 早期版本把它们当成「非表格行」重置了 current，结果表头和数据行被
            # 拆进两张表，整块序列全空。
            if stripped.startswith("├") or stripped.startswith("└"):
                continue
            if not stripped.startswith("│"):
                current = None
                continue
            cells = [cell.strip() for cell in stripped.split("│")[1:-1]]
            if len(cells) < 2:
                continue
            if current is None:
                current = {"header": None, "rows": []}
                tables.append(current)
            if current["header"] is None and cls._PERIOD_RE.match(cells[1]):
                current["header"] = cells
            else:
                current["rows"].append(cells)
        return tables

    @staticmethod
    def _same_numbers(left: Sequence[str], right: Sequence[str]) -> bool:
        if len(left) != len(right):
            return False
        for a, b in zip(left, right):
            x, y = StockDataClient._number(a), StockDataClient._number(b)
            if x is None or y is None:
                if a != b:
                    return False
                continue
            if abs(x - y) > 1e-6:
                return False
        return True

    @classmethod
    def _adopt_header(cls, orphan: Mapping[str, Any],
                      known: Sequence[Mapping[str, Any]]) -> list[str] | None:
        """给缺列头的表借一列头——只在同名行数值完全一致时才认。

        F10 文件是按字节切片分块的，「财务分析」块的第一张表常常丢掉列头
        （曲线救国去猜年份是绝对不行的：年份错了整条走势就是假数据）。这里
        拿它和同块内列头完好的表比对锚点指标（资产负债比率/流动比率/销售毛利
        率），数值逐列相同才采用对方的列头，否则返回 None、整张表丢弃。
        """
        rows = [row for row in (orphan.get("rows") or []) if row and row[0]]
        if not rows:
            return None
        # 不要求整张表等宽：切片切断的那一行会短两个格子（首格只剩 ``53``），
        # 一旦拿它的长度当基准，真正能对上号的锚点行反而被自己的宽度检查挡掉。
        # 所以逐行比对，宽度以**候选表的列头**为准。
        for candidate in known:
            header = candidate.get("header")
            if not header:
                continue
            others = [row for row in (candidate.get("rows") or []) if row and len(row) == len(header)]
            for row in rows:
                if row[0] not in cls._FINANCE_HEADER_ANCHORS or len(row) != len(header):
                    continue
                match = next((other for other in others if other[0] == row[0]), None)
                if match is not None and cls._same_numbers(row[1:], match[1:]):
                    return list(header)
        return None

    @staticmethod
    def _metric_value(label: str, table_unit: str, value: float) -> float:
        """换算到上屏单位。金额一律亿元，比率/周转率/每股指标保持原值。

        单位有两个来源：报告期指标表的**行名**自带 ``(万)``/``(元)``，而三大
        报表整表的**表头**写的是 ``资产负债指标(万元)``。比率行（``(%)``）无论
        落在哪张表里都不能换算——把 12.48% 变成 0.0012 是最难查的那类错。
        """
        if any(token in label for token in ("(%)", "(元)", "(次)", "(万股)")):
            return round(value, 4)
        if "万" in label or table_unit in ("万元", "万股"):
            return round(value * 1e-4, 2)
        return round(value, 4)

    @classmethod
    def _series_from_tables(cls, tables: Sequence[Mapping[str, Any]],
                            *, annual: bool) -> dict[str, Any]:
        """把若干张指标表合并成 ``{"报告期": [...], "指标": {名: [值]}}``（升序）。"""
        columns: dict[str, int] = {}
        metrics: dict[str, dict[str, float]] = {}
        for table in tables:
            header = table.get("header") or []
            index = {header[i]: i for i in range(1, len(header))
                     if cls._PERIOD_RE.match(header[i]) and (not annual or header[i].endswith("-12-31"))}
            if len(index) < 3:
                continue
            table_unit = "万元" if "万元" in header[0] else "万股" if "万股" in header[0] else ""
            for period in index:
                columns.setdefault(period, len(columns))
            for row in (table.get("rows") or []):
                if len(row) != len(header) or not row[0]:
                    continue
                name = cls._FINANCE_METRICS.get(row[0])
                if not name:
                    continue
                bucket = metrics.setdefault(name, {})
                for period, column in index.items():
                    value = cls._number(row[column])
                    if value is not None:
                        bucket.setdefault(period, cls._metric_value(row[0], table_unit, value))
        periods = sorted(columns)
        series = {name: [bucket.get(period) for period in periods] for name, bucket in metrics.items()}
        # 只有一个点的指标画不出走势，直接不要——留着只会让模型以为它是趋势。
        series = {name: values for name, values in series.items()
                  if sum(value is not None for value in values) >= 2}
        if not series:
            return {}
        ordered = {name: series[name] for name in cls._FINANCE_PRIORITY if name in series}
        for name, values in series.items():
            ordered.setdefault(name, values)
        return {"报告期": periods, "指标": ordered}

    @classmethod
    def trim_history_for_prompt(cls, history: Any, max_metrics: int = 12) -> dict[str, Any]:
        """给 prompt 的精简版财务历史：只留最常讨论的指标。

        全量序列有 60+ 个指标，整包塞进 prompt 只会稀释注意力；图表层拿的是
        全量（按指标名精确取值），这里按 :data:`_FINANCE_PRIORITY` 取前面一批，
        让模型把注意力花在真正会被讨论的数字上。
        """
        if not isinstance(history, Mapping):
            return {}
        trimmed: dict[str, Any] = {}
        for scope in ("年度", "季度"):
            series = history.get(scope)
            if not isinstance(series, Mapping):
                continue
            metrics = series.get("指标") if isinstance(series.get("指标"), Mapping) else {}
            order = [name for name in cls._FINANCE_PRIORITY if name in metrics]
            extras = [name for name in metrics if name not in order]
            keep = set((order + extras)[:max_metrics])
            trimmed[scope] = {"报告期": list(series.get("报告期") or []),
                              "指标": {name: metrics[name] for name in metrics if name in keep}}
        if isinstance(history.get("科目变动"), Mapping):
            trimmed["科目变动"] = history["科目变动"]
        return trimmed

    @classmethod
    def _parse_metric_changes(cls, text: str) -> dict[str, Any]:
        """【6.指标变动说明】：三表关键科目的本期/上期数值与变动率。

        TDX 把长科目名按显示宽度折成多行（``经营活动产生`` / ``的现金流量净``
        / ``额``），这里把数值列全空的续行接回上一条，否则现金流量表那三行
        会变成三个毫无意义的词。
        """
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines) if line.strip().startswith("【6.")), -1)
        if start < 0:
            return {}
        period, rows = "", []
        for line in lines[start:start + 160]:
            stripped = line.strip()
            if stripped.startswith("截止日期"):
                if rows:
                    break  # 只要最新一期
                period = re.split(r"[:：]", stripped, maxsplit=1)[-1].strip()
                continue
            if not stripped.startswith("│"):
                continue
            cells = [cell.strip() for cell in stripped.split("│")[1:-1]]
            if len(cells) < 5 or "变动科目" in cells[0]:
                continue
            current, previous, rate = (cls._number(cells[2]), cls._number(cells[3]), cls._number(cells[4]))
            if current is None and previous is None:
                if rows and cells[0]:
                    rows[-1]["科目"] += cells[0]
                continue
            if not cells[0]:
                continue
            rows.append({"科目": cells[0],
                         "本期(亿元)": round(current * 1e-4, 2) if current is not None else None,
                         "上期(亿元)": round(previous * 1e-4, 2) if previous is not None else None,
                         "变动率(%)": rate})
        return {"报告期": period, "明细": rows} if rows else {}

    @classmethod
    def _parse_finance_analysis(cls, text: str) -> dict[str, Any]:
        """「财务分析」块 → 年度序列 + 季度序列 + 三表科目变动。

        全部来自 TDX 原文；解析不出来的部分就不出现（图表层拿不到数据会退回
        别的图，而不是画一条编出来的线）。
        """
        tables = cls._box_tables(text)
        with_header = [table for table in tables if table["header"]]
        for orphan in (table for table in tables if not table["header"]):
            adopted = cls._adopt_header(orphan, with_header)
            if adopted:
                orphan["header"] = adopted
                with_header.append(orphan)
        annual_tables, quarterly_tables = [], []
        for table in with_header:
            header = table["header"]
            periods = [i for i in range(1, len(header)) if cls._PERIOD_RE.match(header[i])]
            if len(periods) < 3:
                continue
            if sum(1 for i in periods if header[i].endswith("-12-31")) >= 3:
                annual_tables.append(table)
            else:
                quarterly_tables.append(table)
        result: dict[str, Any] = {}
        for key, tables_for_key, annual in (("年度", annual_tables, True), ("季度", quarterly_tables, False)):
            series = cls._series_from_tables(tables_for_key, annual=annual)
            if series:
                result[key] = series
        changes = cls._parse_metric_changes(text)
        if changes:
            result["科目变动"] = changes
        return result

    _EXECUTIVE_TITLES = ("董事长", "总经理", "财务总监", "董秘", "副总经理")

    @classmethod
    def _parse_executives(cls, text: str, limit: int = 6) -> list[dict[str, str]]:
        """Keep the officers viewers care about, from 【2.高管列表】 only.

        Scoped to that sub-section because 高管兼职 lists the same people with
        outside titles (独立董事 of other companies), which would misattribute
        a role that is not theirs at this company.
        """
        people: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        # The marker must include the 【】 brackets: every block starts with a
        # "★本栏包括【1.高管持股变动】【2.高管列表】…" index line that also contains
        # the bare marker "2.高管列表", and matching it would slice an empty body.
        for line in cls._slice_subsection(text, "【2.高管列表】").splitlines():
            cells = [cell.strip() for cell in re.split(r"\s{2,}", line.strip()) if cell.strip()]
            if len(cells) < 3:
                continue
            name, title = cells[0], cells[2]
            if not name or len(name) > 4 or not any(word in title for word in cls._EXECUTIVE_TITLES):
                continue
            if (name, title) in seen:
                continue
            seen.add((name, title))
            people.append({"姓名": name, "职务": title})
            if len(people) >= limit:
                break
        return people

    @staticmethod
    def _parse_industry_ranking(text: str, stock_name: str = "") -> dict[str, Any]:
        """研究行业 + 同行家数, e.g. 酿酒(共36家), plus the stock's own rows in
        the four top-30 ranking tables (市场表现/公司规模/估值水平/财务状况).

        Each ranking table is whitespace-aligned: ``1  澳弘电子  42.30 …`` under
        a header line starting with ``排名``.  Only rows naming this stock are
        kept — a stock outside the top-30 simply has no entry for that dimension,
        which the chart renders as "30名开外" instead of a guessed position.
        """
        match = re.search(r"所属研究行业[:：]\s*([^\s(（]+)\s*[（(]共\s*(\d+)\s*家[)）]", text)
        if not match:
            return {}
        ranking: dict[str, Any] = {"研究行业": match.group(1).strip(), "同行家数": int(match.group(2))}
        name = stock_name.strip()
        if not name:
            return ranking
        for tag, label in (("【2.市场表现排名】", "市场表现"), ("【3.公司规模排名】", "公司规模"),
                           ("【4.估值水平排名】", "估值水平"), ("【5.财务状况排名】", "财务状况")):
            block = StockDataClient._slice_subsection(text, tag)
            lines = [line for line in block.splitlines() if line.strip() and "─" not in line]
            header = next((line for line in lines if line.strip().startswith("排名")), "")
            columns = [c for c in header.split()[2:]] if header else []
            for line in lines:
                tokens = line.split()
                # The ranking tables use the short name (澳弘电子) while 公司概况
                # carries the registered name (苏州澳弘电子股份有限公司) — match
                # either direction, short names are always a substring.
                if len(tokens) < 2 or not tokens[0].isdigit():
                    continue
                if not (tokens[1] == name or tokens[1] in name or name in tokens[1]):
                    continue
                entry: dict[str, Any] = {"排名": int(tokens[0])}
                for column, value in zip(columns, tokens[2:]):
                    if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
                        entry[column] = float(value)
                ranking[label] = entry
                break
        return ranking

    @classmethod
    def _parse_themes(cls, text: str, limit: int = 14) -> dict[str, list[str]]:
        """概念/风格 tags from 热点题材【1.所属板块】.

        指数 is dropped: it only lists index memberships (沪深300/上证50 …),
        says nothing about the business, and is by far the longest line.
        """
        result: dict[str, list[str]] = {}
        current = ""
        # Same bracket rule as 高管列表 — see the note there.
        for line in cls._slice_subsection(text, "【1.所属板块】").splitlines():
            stripped = line.strip()
            if not stripped or "──" in stripped:
                continue
            if re.match(r"^指数[:：]", stripped):
                break  # index memberships are not business tags, and they come last
            match = re.match(r"^(概念|风格)[:：]\s*(.+)$", stripped)
            if match:
                current = match.group(1)
                result.setdefault(current, []).extend(cls._split_tags(match.group(2)))
            elif current and not stripped.startswith("【") and result.get(current):
                # A continuation line breaks a tag mid-word (「北上」+「重仓、券商
                # 金股…」), so the tail is rejoined to the last tag and the whole
                # string re-split — otherwise 北上 and 重仓 become two junk tags.
                bucket = result[current]
                bucket[-1:] = cls._split_tags(bucket[-1] + stripped)
        return {key: value[:limit] for key, value in result.items() if value}

    @staticmethod
    def _split_tags(values: str) -> list[str]:
        """Split a 概念/风格 line on the separators TDX uses, dropping 「无」."""
        return [tag.strip() for tag in re.split(r"[、,，]", values)
                if tag.strip() and tag.strip() != "无"]

    @staticmethod
    def _slice_subsection(text: str, marker: str) -> str:
        """Return one F10 sub-section, e.g. the body of 【2.高管列表】.

        The marker must match at the start of a line: every block opens with a
        "★本栏包括【1.…】【2.…】…" index line that also *contains* the marker,
        and matching that line would slice an empty body.
        """
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines) if line.strip().startswith(marker)), -1)
        if start < 0:
            return ""
        end = next((i for i, line in enumerate(lines[start + 1:], start + 1)
                    if line.strip().startswith("【")), len(lines))
        return "\n".join(lines[start:end])

    def get_news(self, code: str | int) -> dict[str, Any]:
        """Fetch recent news/research items for one stock when the CLI supports it.

        Items are trimmed to the fields useful as creative material for the
        dialogue generator; raw payloads vary slightly across tongstock
        releases, so every field is defensively defaulted.
        """
        symbol = normalize_code(code)
        argv = ["news", "query", symbol, "--limit", str(max(1, self.config.news_limit)), "--json"]
        if self.config.news_days > 0:
            argv += ["--days", str(self.config.news_days)]
        payload = self._expect_mapping(self._invoke(*argv))
        items = payload.get("items", []) if isinstance(payload, Mapping) else []
        if not isinstance(items, list):
            items = []
        return {
            "code": symbol,
            "items": [normalized for item in items if isinstance(item, Mapping)
                      and (normalized := self._normalize_news(item))["title"]],
        }

    @staticmethod
    def _normalize_news(item: Mapping[str, Any]) -> dict[str, Any]:
        tags = [str(tag) for tag in item.get("tags", []) if tag] if isinstance(item.get("tags"), list) else []
        return {
            "title": str(item.get("title") or "").strip(),
            "summary": str(item.get("summary") or "").strip(),
            "source": str(item.get("source") or "").strip(),
            "type": str(item.get("newsType") or "").strip(),
            "publish_time": str(item.get("publishTime") or "")[:10],
            "url": str(item.get("url") or "").strip(),
            "tags": tags,
        }

    def get_kline(self, code: str | int, *, count: int = 60) -> list[dict[str, Any]]:
        """Fetch OHLCV bars when supported by the installed CLI."""
        if count < 1:
            raise ValueError("count must be positive")
        symbol = normalize_code(code)
        payload = self._parse_payload(self._invoke("kline", "--code", symbol, "--count", str(count), "--json"))
        rows = payload.get("items", payload.get("data", payload.get("history", []))) if isinstance(payload, Mapping) else payload
        if not isinstance(rows, list):
            raise StockDataError("tongstock kline output has no list of bars")
        return [self._normalize_bar(row) for row in rows if isinstance(row, Mapping)]

    def _invoke(self, command: str, *args: str) -> str:
        argv = (self.config.executable, command, *args, "--consistency", self.config.consistency)
        last_error: Exception | None = None
        for attempt in range(self.config.retries + 1):
            try:
                return self._runner(argv, self.config.timeout_seconds)
            except (OSError, subprocess.SubprocessError, TongstockCommandError) as exc:
                last_error = exc
                message = str(exc)
                if "unknown command" in message.lower():
                    raise TongstockUnavailableError(f"tongstock does not support `{command}` in this installed version") from exc
                if attempt < self.config.retries:
                    time.sleep(self.config.retry_delay_seconds * (2**attempt))
        raise TongstockCommandError(f"tongstock {command} failed after {self.config.retries + 1} attempts: {last_error}") from last_error

    @staticmethod
    def _run_subprocess(argv: Sequence[str], timeout: float) -> str:
        try:
            completed = subprocess.run(argv, check=True, text=True, capture_output=True, timeout=timeout)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise TongstockCommandError(detail) from exc
        return completed.stdout

    @staticmethod
    def _parse_payload(raw: str) -> Any:
        # tongstock may prepend connection/sync log lines before JSON.
        start = min((i for i in (raw.find("{"), raw.find("[")) if i >= 0), default=-1)
        if start < 0:
            return None
        try:
            decoder = json.JSONDecoder()
            return decoder.raw_decode(raw[start:])[0]
        except json.JSONDecodeError:
            return None

    def _expect_mapping(self, raw: str) -> dict[str, Any]:
        payload = self._parse_payload(raw)
        if not isinstance(payload, Mapping):
            raise StockDataError("tongstock returned non-JSON data where JSON was required")
        return dict(payload)

    @staticmethod
    def _normalize_quote(data: Mapping[str, Any], code: str) -> dict[str, Any]:
        aliases = {"price": ("price", "current", "last", "close"), "open": ("open",), "high": ("high",), "low": ("low",), "volume": ("volume", "vol"), "amount": ("amount", "turnover"), "change_pct": ("change_pct", "pct_chg", "percent")}
        normalized = {"code": str(data.get("code", code)), "name": str(data.get("name", ""))}
        for key, names in aliases.items():
            normalized[key] = next((StockDataClient._number(data.get(name)) for name in names if data.get(name) is not None), None)
        return normalized

    @staticmethod
    def _parse_text_quote(raw: str, code: str) -> dict[str, Any]:
        values = {"price": r"最新价:\s*([\d.]+)", "open": r"开盘:\s*([\d.]+)", "high": r"最高:\s*([\d.]+)", "low": r"最低:\s*([\d.]+)", "volume": r"成交量:\s*([\d.]+)", "amount": r"成交额:\s*([\d.]+)"}
        # The name (if any) sits on the same line as the code — never match
        # across the newline into field labels like "最新价:".
        name_match = re.search(rf"^{re.escape(code)}[ \t]+(\S+)[ \t]*$", raw, re.MULTILINE)
        name = name_match.group(1) if name_match else ""
        return {"code": code, "name": name, **{key: StockDataClient._number(match.group(1)) if (match := re.search(pattern, raw)) else None for key, pattern in values.items()}, "change_pct": None}

    def _resolve_name(self, code: str) -> str:
        """Best-effort company name from the securities list (real data only).

        The codes list prints CJK names with spaces between every character,
        e.g. ``002224 三 力 士 [深市主板] 深交所``; capture the name column and
        collapse those spaces, stopping before the ``[板块]`` tag.
        """
        try:
            raw = self._invoke("codes", "list", "-e", self._exchange_of(code))
        except (TongstockUnavailableError, TongstockCommandError, StockDataError):
            return ""
        match = re.search(rf"^{re.escape(code)}\s+(.+?)(?:\s*\[[^\]]*\].*)?$", raw, re.MULTILINE)
        return re.sub(r"\s+", "", match.group(1)) if match else ""

    @staticmethod
    def _exchange_of(code: str) -> str:
        """Map a securities code to its exchange flag for ``codes list``."""
        if code.startswith(("6", "9")) or code.endswith((".SH", ".SS")):
            return "sh"
        if code.startswith(("4", "8")) or code.endswith((".BJ",)):
            return "bj"
        return "sz"

    @staticmethod
    def _normalize_bar(row: Mapping[str, Any]) -> dict[str, Any]:
        # tongstock kline uses "time" for the date; older indicator history
        # uses "timestamp" or "date".  Normalise to "date" for downstream.
        bar = {key: row.get(key) for key in ("open", "high", "low", "close", "volume", "amount") if key in row}
        bar["date"] = row.get("date") or row.get("time") or row.get("timestamp")
        return bar

    @staticmethod
    def _number(value: Any, default: Any = None) -> float | Any:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, Mapping) else {}


def get_stock_data(code: str | int, *, kline_count: int = 60) -> dict[str, Any]:
    """Convenience entry point used by the pipeline."""
    return StockDataClient().get_stock_data(code, kline_count=kline_count)
