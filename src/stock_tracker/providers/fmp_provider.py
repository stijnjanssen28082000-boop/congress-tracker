"""FundamentalsDataProvider backed by Financial Modeling Prep (FMP)."""

from __future__ import annotations

import os
from datetime import date, datetime

import requests

from stock_tracker.providers.base import (
    EarningsEvent,
    EstimateSnapshot,
    FundamentalsDataProvider,
    FundamentalsSnapshot,
)

BASE_URL = "https://financialmodelingprep.com/api/v3"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


class FMPProvider(FundamentalsDataProvider):
    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.api_key = api_key or os.environ.get("FMP_API_KEY")
        if not self.api_key:
            raise ValueError("FMP_API_KEY is not set (env var or constructor argument)")
        self.session = session or requests.Session()

    def _get(self, path: str, **params) -> list[dict]:
        params["apikey"] = self.api_key
        response = self.session.get(f"{BASE_URL}/{path}", params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else []

    def get_fundamentals(self, ticker: str) -> list[FundamentalsSnapshot]:
        income = self._get(f"income-statement/{ticker}", period="quarter", limit=48)
        cash_flow = self._get(f"cash-flow-statement/{ticker}", period="quarter", limit=48)
        balance_sheet = self._get(f"balance-sheet-statement/{ticker}", period="quarter", limit=48)
        enterprise_values = self._get(
            f"enterprise-values/{ticker}", period="quarter", limit=48
        )

        fcf_by_period = {row["date"]: row.get("freeCashFlow") for row in cash_flow}
        net_debt_by_period = {row["date"]: row.get("netDebt") for row in balance_sheet}
        market_cap_by_period = {
            row["date"]: row.get("marketCapitalization") for row in enterprise_values
        }

        snapshots = []
        for row in income:
            period_end = _parse_date(row.get("date"))
            report_date = _parse_date(row.get("fillingDate") or row.get("acceptedDate"))
            if period_end is None or report_date is None:
                continue
            snapshots.append(
                FundamentalsSnapshot(
                    period_end=period_end,
                    report_date=report_date,
                    revenue=row.get("revenue"),
                    free_cash_flow=fcf_by_period.get(row["date"]),
                    net_debt=net_debt_by_period.get(row["date"]),
                    ebitda=row.get("ebitda"),
                    market_cap=market_cap_by_period.get(row["date"]),
                )
            )
        return sorted(snapshots, key=lambda s: s.period_end)

    def get_estimates(self, ticker: str) -> list[EstimateSnapshot]:
        # FMP's free/standard tier only exposes the *current* consensus estimate
        # per fiscal year, not a historical point-in-time series. Point-in-time
        # history for the quality filter (Module 2) is built by storing a new
        # snapshot each time ingest runs, using today's date as `as_of_date`.
        rows = self._get(f"analyst-estimates/{ticker}", period="annual", limit=8)
        today = date.today()
        estimates = []
        for row in rows:
            fiscal_date = _parse_date(row.get("date"))
            eps_estimate = row.get("estimatedEpsAvg")
            if fiscal_date is None or eps_estimate is None:
                continue
            estimates.append(
                EstimateSnapshot(
                    as_of_date=today,
                    fiscal_year=fiscal_date.year,
                    eps_estimate=float(eps_estimate),
                )
            )
        return estimates

    def get_earnings_calendar(self, ticker: str) -> list[EarningsEvent]:
        rows = self._get(f"historical/earning_calendar/{ticker}")
        events = []
        for row in rows:
            earnings_date = _parse_date(row.get("date"))
            if earnings_date is None:
                continue
            events.append(
                EarningsEvent(
                    earnings_date=earnings_date,
                    confirmed=row.get("epsEstimated") is not None or row.get("eps") is not None,
                )
            )
        return sorted(events, key=lambda e: e.earnings_date)
