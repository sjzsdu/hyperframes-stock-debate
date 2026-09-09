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


Runner = Callable[[Sequence[str], float], str]


@dataclass(frozen=True)
class StockDataConfig:
    """Runtime policy for :class:`StockDataClient`."""

    executable: str = "tongstock"
    retries: int = 2
    retry_delay_seconds: float = 0.35
    timeout_seconds: float = 20.0
    consistency: str = "allow_stale"
    f10_blocks: tuple[str, ...] = ("公司概况", "经营分析", "财务分析", "股东研究")


def normalize_code(code: str | int) -> str:
    """Return a six-digit mainland A-share code or raise :class:`ValueError`."""
    value = str(code).strip().upper()
    value = re.sub(r"^(SH|SZ|BJ)", "", value)
    value = re.sub(r"\.(SH|SZ|BJ)$", "", value)
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("A-share code must be a six-digit code, e.g. '600519'")
    return value


class StockDataClient:
    """Fetch quote, F10, financial, indicator and K-line data from tongstock."""

    def __init__(self, config: StockDataConfig | None = None, runner: Runner | None = None) -> None:
        self.config = config or StockDataConfig()
        self._runner = runner or self._run_subprocess

    def get_stock_data(self, code: str | int, *, kline_count: int = 60) -> dict[str, Any]:
        """Fetch all available datasets and return one stable video-ready payload.

        A missing optional F10/finance/K-line command does not discard current quote
        and technical data; its error is recorded in ``unavailable`` so the
        script generator can describe only data that is actually present.
        """
        symbol = normalize_code(code)
        result: dict[str, Any] = {
            "code": symbol,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "quote": self.get_quote(symbol),
            "technical": self.get_indicators(symbol, count=kline_count),
            "financials": None,
            "f10": None,
            "kline": [],
            "unavailable": {},
        }
        for name, operation in (
            ("f10", self.get_f10),
            ("financials", self.get_financials),
            ("kline", lambda c: self.get_kline(c, count=kline_count)),
        ):
            try:
                result[name] = operation(symbol)
            except TongstockUnavailableError as exc:
                result["unavailable"][name] = str(exc)
        return result

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
        """Fetch finance JSON when supported by the installed CLI."""
        symbol = normalize_code(code)
        return self._expect_mapping(self._invoke("finance", "--code", symbol, "--json"))

    def get_f10(self, code: str | int) -> dict[str, Any]:
        """Fetch the F10 catalogue and configured content blocks.

        F10 formats vary by tongstock release, so JSON blocks are decoded while
        text blocks are retained verbatim.  Individual blocks fail independently
        to retain all other company context for the dialogue generator.
        """
        symbol = normalize_code(code)
        directory_raw = self._invoke("company", symbol)
        directory_payload = self._parse_payload(directory_raw)
        sections: dict[str, Any] = {}
        unavailable: dict[str, str] = {}
        for block in self.config.f10_blocks:
            try:
                raw = self._invoke("company-content", symbol, "--block", block, "--length", "10000")
                parsed = self._parse_payload(raw)
                sections[block] = parsed if parsed is not None else raw.strip()
            except (TongstockUnavailableError, TongstockCommandError, StockDataError) as exc:
                unavailable[block] = str(exc)
        return {
            "directory": directory_payload if directory_payload is not None else directory_raw.strip(),
            "sections": sections,
            "unavailable_sections": unavailable,
        }

    def get_kline(self, code: str | int, *, count: int = 60) -> list[dict[str, Any]]:
        """Fetch OHLCV bars when supported by the installed CLI."""
        if count < 1:
            raise ValueError("count must be positive")
        symbol = normalize_code(code)
        payload = self._parse_payload(self._invoke("kline", "--code", symbol, "--count", str(count), "--json"))
        rows = payload.get("data", payload.get("history", [])) if isinstance(payload, Mapping) else payload
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
        return {"code": code, "name": "", **{key: StockDataClient._number(match.group(1)) if (match := re.search(pattern, raw)) else None for key, pattern in values.items()}, "change_pct": None}

    @staticmethod
    def _normalize_bar(row: Mapping[str, Any]) -> dict[str, Any]:
        return {key: row.get(key) for key in ("date", "timestamp", "open", "high", "low", "close", "volume", "amount") if key in row}

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
