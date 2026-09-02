"""PriceDataProvider backed by yfinance."""

from __future__ import annotations

from datetime import date, timedelta

import yfinance as yf

from stock_tracker.providers.base import PriceBar, PriceDataProvider


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
        for row_date, row in history.iterrows():
            bars.append(
                PriceBar(
                    date=row_date.date(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]),
                    currency=currency,
                )
            )
        return bars
