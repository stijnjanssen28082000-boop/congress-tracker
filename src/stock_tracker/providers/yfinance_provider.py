"""PriceDataProvider backed by yfinance."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from stock_tracker.providers.base import PriceBar, PriceDataProvider

logger = logging.getLogger("stock_tracker.providers.yfinance")

_OHLC_COLUMNS = ("Open", "High", "Low", "Close")


class YFinanceProvider(PriceDataProvider):
    def get_prices(self, ticker: str, start: date, end: date) -> list[PriceBar]:
        yf_ticker = yf.Ticker(ticker)
        # yfinance's `end` is exclusive; the interface's `end` is inclusive.
        history = yf_ticker.history(start=start, end=end + timedelta(days=1), auto_adjust=False)
        if history.empty:
            return []

        try:
            currency = yf_ticker.fast_info.get("currency") or "USD"
        except Exception:
            currency = "USD"

        bars = []
        skipped = 0
        for row_date, row in history.iterrows():
            # yfinance occasionally returns an all-NaN row for a date it has no
            # actual trade data for (e.g. a market holiday it doesn't know
            # about). Storing that would violate the NOT NULL price columns.
            if row[list(_OHLC_COLUMNS)].isna().any():
                skipped += 1
                continue
            bars.append(
                PriceBar(
                    date=row_date.date(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
                    currency=currency,
                )
            )
        if skipped:
            logger.warning("%s: skipped %d price row(s) with missing OHLC data", ticker, skipped)
        return bars
