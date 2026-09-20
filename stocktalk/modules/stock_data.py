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
    # 经营分析 (主营构成与毛利率), 高管治理 (谁在管), 行业分析 (行业地位与同行),
    # 热点题材 (概念/风格标签).  财务分析 is deliberately absent: `finance`
    # already returns those numbers as JSON, and the block alone is ~38K chars
    # of table text that only crowds the prompt.
    f10_blocks: tuple[str, ...] = ("公司概况", "经营分析", "高管治理", "行业分析", "热点题材")
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
            # `quote` often returns no name; `block show` echoes one as part of
            # its header, and it is the same exchange source.
            board = result.get("board") if isinstance(result.get("board"), Mapping) else {}
            result["quote"]["name"] = str(board.get("name") or "") or self._resolve_name(symbol)
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
        for source, label in (("公司名称", "公司名称"), ("主营业务", "主营业务"),
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
