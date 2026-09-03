"""FundamentalsDataProvider backed by yfinance (Yahoo Finance).

Free fallback for FMP's fundamentals endpoints, which the free FMP tier
doesn't expose (403 Forbidden on `income-statement`/`analyst-estimates`).
Unlike FMP's free tier, this needs no API key and covers both US and
European tickers — the same source already used for price data.

Trade-offs vs. FMP, documented so they're easy to revisit:
  - `report_date` is approximated as `period_end + 45 days` (typical filing
    lag) since Yahoo doesn't expose an actual filing date.
  - `market_cap` is today's market cap applied to every snapshot returned in
    one call, not a true point-in-time historical figure (Yahoo doesn't
    expose historical market cap for free). Once a period is stored,
    `_store_fundamentals` never updates it, so a period's market_cap is
    effectively "whatever it was when first ingested" going forward.
  - Analyst EPS estimates only cover the annual rows Yahoo labels "0y"
    (current fiscal year) and "+1y"/"+2y" (future fiscal years); the fiscal
    year number itself is approximated as `today.year + offset`, which can
    be off by one for companies whose fiscal year doesn't match the
    calendar year.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from stock_tracker.providers.base import (
    EarningsEvent,
    EstimateSnapshot,
    FundamentalsDataProvider,
    FundamentalsSnapshot,
)

logger = logging.getLogger("stock_tracker.providers.yfinance_fundamentals")

_REPORT_LAG_DAYS = 45
_YEAR_LABEL_RE = re.compile(r"^\+?(-?\d+)y$")


def _row_value(df: pd.DataFrame | None, column, candidates: list[str]) -> float | None:
    if df is None or df.empty or column not in df.columns:
        return None
    for name in candidates:
        if name in df.index:
            value = df.at[name, column]
            if value is not None and not pd.isna(value):
                return float(value)
    return None


def _period_label_to_fiscal_year(label: str, today: date) -> int | None:
    match = _YEAR_LABEL_RE.match(str(label).strip())
    if not match:
        return None
    return today.year + int(match.group(1))


class YFinanceFundamentalsProvider(FundamentalsDataProvider):
    def _ticker(self, ticker: str) -> yf.Ticker:
        return yf.Ticker(ticker)

    def _current_market_cap(self, yf_ticker: yf.Ticker) -> float | None:
        try:
            value = yf_ticker.fast_info.get("market_cap")
            return float(value) if value is not None else None
        except Exception:
            return None

    def get_fundamentals(self, ticker: str) -> list[FundamentalsSnapshot]:
        yf_ticker = self._ticker(ticker)
        try:
            income = yf_ticker.quarterly_income_stmt
            balance = yf_ticker.quarterly_balance_sheet
            cashflow = yf_ticker.quarterly_cashflow
        except Exception:
            logger.exception("Failed to fetch financial statements for %s", ticker)
            return []

        if income is None or income.empty:
            return []

        market_cap = self._current_market_cap(yf_ticker)

        snapshots = []
        for column in income.columns:
            try:
                period_end = column.date()
            except AttributeError:
                continue

            revenue = _row_value(income, column, ["Total Revenue"])
            ebitda = _row_value(income, column, ["EBITDA", "Normalized EBITDA"])

            free_cash_flow = _row_value(cashflow, column, ["Free Cash Flow"])
            if free_cash_flow is None:
                operating_cf = _row_value(cashflow, column, ["Operating Cash Flow"])
                capex = _row_value(cashflow, column, ["Capital Expenditure"])
                if operating_cf is not None and capex is not None:
                    # Yahoo stores capex as a negative number already.
                    free_cash_flow = operating_cf + capex

            net_debt = _row_value(balance, column, ["Net Debt"])
            if net_debt is None:
                total_debt = _row_value(balance, column, ["Total Debt"])
                cash = _row_value(
                    balance,
                    column,
                    [
                        "Cash And Cash Equivalents",
                        "Cash Cash Equivalents And Short Term Investments",
                    ],
                )
                if total_debt is not None and cash is not None:
                    net_debt = total_debt - cash

            snapshots.append(
                FundamentalsSnapshot(
                    period_end=period_end,
                    report_date=period_end + timedelta(days=_REPORT_LAG_DAYS),
                    revenue=revenue,
                    free_cash_flow=free_cash_flow,
                    net_debt=net_debt,
                    ebitda=ebitda,
                    market_cap=market_cap,
                )
            )
        return sorted(snapshots, key=lambda s: s.period_end)

    def get_estimates(self, ticker: str) -> list[EstimateSnapshot]:
        yf_ticker = self._ticker(ticker)
        try:
            table = yf_ticker.earnings_estimate
        except Exception:
            logger.exception("Failed to fetch earnings estimates for %s", ticker)
            return []

        if table is None or table.empty or "avg" not in table.columns:
            return []

        today = date.today()
        estimates = []
        for label, row in table.iterrows():
            fiscal_year = _period_label_to_fiscal_year(label, today)
            avg = row.get("avg")
            if fiscal_year is None or avg is None or pd.isna(avg):
                continue
            estimates.append(
                EstimateSnapshot(as_of_date=today, fiscal_year=fiscal_year, eps_estimate=float(avg))
            )
        return estimates

    def get_earnings_calendar(self, ticker: str) -> list[EarningsEvent]:
        yf_ticker = self._ticker(ticker)
        try:
            table = yf_ticker.get_earnings_dates(limit=12)
        except Exception:
            logger.exception("Failed to fetch earnings calendar for %s", ticker)
            return []

        if table is None or table.empty:
            return []

        events = []
        for timestamp in table.index:
            try:
                earnings_date = timestamp.date()
            except AttributeError:
                continue
            events.append(EarningsEvent(earnings_date=earnings_date, confirmed=True))
        return sorted(events, key=lambda e: e.earnings_date)
