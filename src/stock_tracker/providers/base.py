"""DataProvider interface: abstracts data sources so ingest.py never talks
to yfinance or Financial Modeling Prep directly.

Split into two focused interfaces because no single free/cheap source covers
both price history and fundamentals well — `PriceDataProvider` for OHLCV,
`FundamentalsDataProvider` for fundamentals/estimates/earnings dates. Adding a
new source later (e.g. swapping FMP for another vendor) means implementing
one of these two and wiring it in `ingest.py`; nothing else changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PriceBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    currency: str


@dataclass(frozen=True)
class FundamentalsSnapshot:
    period_end: date
    report_date: date
    revenue: float | None
    free_cash_flow: float | None
    net_debt: float | None
    ebitda: float | None
    market_cap: float | None


@dataclass(frozen=True)
class EstimateSnapshot:
    as_of_date: date
    fiscal_year: int
    eps_estimate: float


@dataclass(frozen=True)
class EarningsEvent:
    earnings_date: date
    confirmed: bool


class PriceDataProvider(ABC):
    """Source of daily OHLCV price bars."""

    @abstractmethod
    def get_prices(self, ticker: str, start: date, end: date) -> list[PriceBar]:
        """Returns daily bars for `ticker` in [start, end], in the ticker's
        native currency."""


class FundamentalsDataProvider(ABC):
    """Source of fundamentals, analyst estimates and earnings dates."""

    @abstractmethod
    def get_fundamentals(self, ticker: str) -> list[FundamentalsSnapshot]:
        """Returns quarterly fundamentals snapshots, most recent last."""

    @abstractmethod
    def get_estimates(self, ticker: str) -> list[EstimateSnapshot]:
        """Returns point-in-time analyst EPS estimate snapshots."""

    @abstractmethod
    def get_earnings_calendar(self, ticker: str) -> list[EarningsEvent]:
        """Returns known/confirmed upcoming and past earnings dates."""
