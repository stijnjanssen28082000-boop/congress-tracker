"""Daily FX rates and EUR conversion for non-EUR-denominated prices."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import yfinance as yf

from stock_tracker.db.models import FxRate
from stock_tracker.db.session import get_session

logger = logging.getLogger("stock_tracker.fx")


def fetch_fx_rates(currency: str, start: date, end: date) -> dict[date, float]:
    """Returns {date: rate_to_eur} for `currency` over [start, end].

    `rate_to_eur` is EUR per 1 unit of `currency` — multiply a native-currency
    amount by it to get EUR. EUR itself has no rate series (always 1.0).
    """
    if currency == "EUR":
        return {}

    pair = f"{currency}EUR=X"
    history = yf.Ticker(pair).history(start=start, end=end + timedelta(days=1))
    if history.empty:
        return {}
    return {row_date.date(): float(row["Close"]) for row_date, row in history.iterrows()}


def store_fx_rates(currency: str, rates: dict[date, float]) -> int:
    if not rates:
        return 0
    with get_session() as session:
        existing = {
            r.date: r for r in session.query(FxRate).filter(FxRate.currency == currency).all()
        }
        for rate_date, rate in rates.items():
            existing_row = existing.get(rate_date)
            if existing_row is not None:
                existing_row.rate_to_eur = rate
            else:
                session.add(FxRate(date=rate_date, currency=currency, rate_to_eur=rate))
    return len(rates)


def get_rate_to_eur(currency: str, on_date: date) -> float:
    """Looks up the most recent stored rate on or before `on_date` (weekends/
    holidays fall back to the last known business-day rate). Returns 1.0 for EUR."""
    if currency == "EUR":
        return 1.0
    with get_session() as session:
        rate_row = (
            session.query(FxRate)
            .filter(FxRate.currency == currency, FxRate.date <= on_date)
            .order_by(FxRate.date.desc())
            .first()
        )
        if rate_row is None:
            raise LookupError(f"No FX rate available for {currency} on or before {on_date}")
        return rate_row.rate_to_eur


def convert_to_eur(amount: float, currency: str, on_date: date) -> float:
    return amount * get_rate_to_eur(currency, on_date)
