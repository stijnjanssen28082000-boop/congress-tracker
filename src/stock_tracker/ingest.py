"""CLI entrypoint: `ingest.py --full` (12y history) or `--daily` (incremental).

`--full` refreshes the universe, fetches the full price history, and pulls
fundamentals/estimates/earnings for every ticker. `--daily` only fetches price
bars newer than the latest one stored per ticker, plus a refresh of analyst
estimates and the earnings calendar (small payloads that the signal engine's
earnings-guard and EPS-drop flag need to stay current); full fundamentals
statements only change quarterly and are left to `--full`.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from dotenv import load_dotenv

from stock_tracker.config import Config, load_config
from stock_tracker.db.models import (
    AnalystEstimate,
    EarningsCalendar,
    Fundamentals,
    PriceDaily,
    Ticker,
)
from stock_tracker.db.session import get_session, init_db
from stock_tracker.fx import convert_to_eur, fetch_fx_rates, store_fx_rates
from stock_tracker.logging_setup import setup_logging
from stock_tracker.providers.base import FundamentalsDataProvider, PriceDataProvider
from stock_tracker.providers.fmp_provider import FMPProvider
from stock_tracker.providers.yfinance_fundamentals_provider import YFinanceFundamentalsProvider
from stock_tracker.providers.yfinance_provider import YFinanceProvider
from stock_tracker.universe import build_universe, sync_universe_to_db

logger = logging.getLogger("stock_tracker.ingest")


def _last_price_date(ticker: str) -> date | None:
    with get_session() as session:
        row = (
            session.query(PriceDaily)
            .filter(PriceDaily.ticker == ticker)
            .order_by(PriceDaily.date.desc())
            .first()
        )
        return row.date if row else None


def _store_prices(ticker: str, currency: str, bars) -> int:
    if not bars:
        return 0
    with get_session() as session:
        existing_dates = {
            d for (d,) in session.query(PriceDaily.date).filter(PriceDaily.ticker == ticker).all()
        }
        stored = 0
        for bar in bars:
            if bar.date in existing_dates:
                continue
            close_eur = convert_to_eur(bar.close, currency, bar.date)
            session.add(
                PriceDaily(
                    ticker=ticker,
                    date=bar.date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    close_eur=close_eur,
                )
            )
            stored += 1
    return stored


def _ingest_prices_and_fx(
    ticker_row: Ticker, start: date, end: date, price_provider: PriceDataProvider
) -> int:
    bars = price_provider.get_prices(ticker_row.ticker, start, end)
    if ticker_row.currency != "EUR":
        fx_rates = fetch_fx_rates(ticker_row.currency, start, end)
        store_fx_rates(ticker_row.currency, fx_rates)
    return _store_prices(ticker_row.ticker, ticker_row.currency, bars)


def _store_fundamentals(ticker: str, snapshots) -> int:
    if not snapshots:
        return 0
    with get_session() as session:
        existing_periods = {
            p
            for (p,) in session.query(Fundamentals.period_end)
            .filter(Fundamentals.ticker == ticker)
            .all()
        }
        stored = 0
        for snap in snapshots:
            if snap.period_end in existing_periods:
                continue
            session.add(
                Fundamentals(
                    ticker=ticker,
                    period_end=snap.period_end,
                    report_date=snap.report_date,
                    revenue=snap.revenue,
                    free_cash_flow=snap.free_cash_flow,
                    net_debt=snap.net_debt,
                    ebitda=snap.ebitda,
                    market_cap=snap.market_cap,
                    source="fmp",
                )
            )
            stored += 1
    return stored


def _store_estimates(ticker: str, estimates) -> int:
    if not estimates:
        return 0
    with get_session() as session:
        existing_keys = {
            (as_of, fy)
            for (as_of, fy) in session.query(
                AnalystEstimate.as_of_date, AnalystEstimate.fiscal_year
            )
            .filter(AnalystEstimate.ticker == ticker)
            .all()
        }
        stored = 0
        for est in estimates:
            if (est.as_of_date, est.fiscal_year) in existing_keys:
                continue
            session.add(
                AnalystEstimate(
                    ticker=ticker,
                    as_of_date=est.as_of_date,
                    fiscal_year=est.fiscal_year,
                    eps_estimate=est.eps_estimate,
                    source="fmp",
                )
            )
            stored += 1
    return stored


def _store_earnings(ticker: str, events) -> int:
    if not events:
        return 0
    with get_session() as session:
        existing_dates = {
            d
            for (d,) in session.query(EarningsCalendar.earnings_date)
            .filter(EarningsCalendar.ticker == ticker)
            .all()
        }
        stored = 0
        for event in events:
            if event.earnings_date in existing_dates:
                continue
            session.add(
                EarningsCalendar(
                    ticker=ticker,
                    earnings_date=event.earnings_date,
                    confirmed=event.confirmed,
                    source="fmp",
                )
            )
            stored += 1
    return stored


def _build_fundamentals_provider(config: Config) -> FundamentalsDataProvider | None:
    provider_name = config.ingest.fundamentals_provider
    if provider_name == "yfinance":
        return YFinanceFundamentalsProvider()
    if provider_name == "fmp":
        try:
            return FMPProvider()
        except ValueError:
            logger.warning(
                "FMP_API_KEY not set — skipping fundamentals/estimates/earnings ingest"
            )
            return None
    raise ValueError(f"Unknown ingest.fundamentals_provider: {provider_name!r}")


def _refresh_fundamentals(ticker: str, provider: FundamentalsDataProvider, full: bool) -> None:
    try:
        if full:
            _store_fundamentals(ticker, provider.get_fundamentals(ticker))
        _store_estimates(ticker, provider.get_estimates(ticker))
        _store_earnings(ticker, provider.get_earnings_calendar(ticker))
    except Exception:
        logger.exception("Fundamentals ingest failed for %s", ticker)


def run_full(config: Config) -> None:
    logger.info("Starting full ingest (%s years history)", config.ingest.full_history_years)
    init_db(config)

    universe = build_universe()
    sync_universe_to_db(universe)
    logger.info("Universe synced: %d tickers", len(universe))

    price_provider = YFinanceProvider()
    fundamentals_provider = _build_fundamentals_provider(config)

    end = date.today() - timedelta(days=1)
    start = end.replace(year=end.year - config.ingest.full_history_years)

    with get_session() as session:
        tickers = session.query(Ticker).all()

    for ticker_row in tickers:
        try:
            stored = _ingest_prices_and_fx(ticker_row, start, end, price_provider)
            logger.info("%s: stored %d new price bars", ticker_row.ticker, stored)
        except Exception:
            logger.exception("Price ingest failed for %s", ticker_row.ticker)

        if fundamentals_provider is not None:
            _refresh_fundamentals(ticker_row.ticker, fundamentals_provider, full=True)


def run_daily(config: Config) -> None:
    logger.info("Starting daily incremental ingest")
    init_db(config)

    universe = build_universe()
    sync_universe_to_db(universe)

    price_provider = YFinanceProvider()
    fundamentals_provider = _build_fundamentals_provider(config)

    end = date.today() - timedelta(days=1)
    fallback_start = end.replace(year=end.year - config.ingest.full_history_years)

    with get_session() as session:
        tickers = session.query(Ticker).all()

    for ticker_row in tickers:
        last_date = _last_price_date(ticker_row.ticker)
        start = (last_date + timedelta(days=1)) if last_date else fallback_start
        if start > end:
            logger.debug("%s: already up to date", ticker_row.ticker)
        else:
            try:
                stored = _ingest_prices_and_fx(ticker_row, start, end, price_provider)
                logger.info("%s: stored %d new price bars", ticker_row.ticker, stored)
            except Exception:
                logger.exception("Price ingest failed for %s", ticker_row.ticker)

        if fundamentals_provider is not None:
            _refresh_fundamentals(ticker_row.ticker, fundamentals_provider, full=False)


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="stock-tracker data ingest")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--full", action="store_true", help="Fetch full history (12 years)")
    group.add_argument("--daily", action="store_true", help="Incrementally update since last run")
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config)

    if args.full:
        run_full(config)
    else:
        run_daily(config)


if __name__ == "__main__":
    main()
