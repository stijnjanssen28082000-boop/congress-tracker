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
from sqlalchemy import text

from stock_tracker.config import Config, load_config
from stock_tracker.db.models import (
    AnalystEstimate,
    EarningsCalendar,
    Fundamentals,
    FxRate,
    PriceDaily,
    Ticker,
)
from stock_tracker.db.session import get_engine, get_session, init_db
from stock_tracker.fx import convert_to_eur, fetch_fx_rates, store_fx_rates
from stock_tracker.logging_setup import setup_logging
from stock_tracker.providers.base import (
    EstimateSnapshot,
    FundamentalsDataProvider,
    PriceDataProvider,
)
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


def _store_fundamentals(ticker: str, snapshots, source: str) -> int:
    if not snapshots:
        return 0
    with get_session() as session:
        existing_by_period = {
            row.period_end: row
            for row in session.query(Fundamentals).filter(Fundamentals.ticker == ticker).all()
        }
        stored = 0
        seen_periods: set = set()
        for snap in snapshots:
            # Also guards against a provider returning the same period twice
            # in one call, not just periods already stored from an earlier run.
            if snap.period_end in seen_periods:
                continue
            seen_periods.add(snap.period_end)

            existing = existing_by_period.get(snap.period_end)
            if existing is not None:
                # Unlike revenue/fcf/net_debt/ebitda (frozen once filed, for
                # backtest point-in-time correctness), market_cap is a
                # "current" snapshot value, not historical -- keep it fresh
                # on every full ingest instead of freezing whatever it
                # happened to be (even None, from a since-fixed bug) the
                # first time this period was stored.
                if snap.market_cap is not None:
                    existing.market_cap = snap.market_cap
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
                    source=source,
                )
            )
            stored += 1
    return stored


def _store_estimates(ticker: str, estimates, source: str) -> int:
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
            key = (est.as_of_date, est.fiscal_year)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            session.add(
                AnalystEstimate(
                    ticker=ticker,
                    as_of_date=est.as_of_date,
                    fiscal_year=est.fiscal_year,
                    eps_estimate=est.eps_estimate,
                    source=source,
                )
            )
            stored += 1
    return stored


def _store_earnings(ticker: str, events, source: str) -> int:
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
            # Some providers (e.g. yfinance) can return the same calendar
            # date twice in one call — dedup against this batch too, not
            # just against rows already stored from an earlier run.
            if event.earnings_date in existing_dates:
                continue
            existing_dates.add(event.earnings_date)
            session.add(
                EarningsCalendar(
                    ticker=ticker,
                    earnings_date=event.earnings_date,
                    confirmed=event.confirmed,
                    source=source,
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


def _refresh_fundamentals(
    ticker: str, provider: FundamentalsDataProvider, full: bool, source: str, as_of: date
) -> None:
    try:
        if full:
            _store_fundamentals(ticker, provider.get_fundamentals(ticker), source)
        # Providers stamp estimates with the moment they were fetched
        # (date.today()), but ingest always fetches prices only through
        # yesterday (today's session may not have closed yet). Re-stamp
        # with the same reference date prices use, so a later point-in-time
        # query "as of <that date>" doesn't see estimates as being from the
        # future relative to the price data fetched in this same run.
        estimates = [
            EstimateSnapshot(
                as_of_date=as_of, fiscal_year=e.fiscal_year, eps_estimate=e.eps_estimate
            )
            for e in provider.get_estimates(ticker)
        ]
        _store_estimates(ticker, estimates, source)
        _store_earnings(ticker, provider.get_earnings_calendar(ticker), source)
    except Exception:
        logger.exception("Fundamentals ingest failed for %s", ticker)


def prune_old_prices(config: Config) -> dict[str, int]:
    """Deletes price/FX history older than `ingest.price_retention_years` and
    reclaims the freed disk space with `VACUUM`.

    Live signal generation and the quality filter only ever look back about a
    year (52-week high, SMA-50, RSI-14, 60-day average volume); the much
    longer `full_history_years` window is for the backtester, which isn't
    part of the automated cloud pipeline — it's a manual, occasional command.
    Keeping the full window in the database that gets committed to git on
    every run isn't needed for that, and blows past GitHub's 100MB per-file
    push limit at this universe's size (a full 529-ticker/12-year database
    hit ~146MB). Run `ingest.py --full` locally, or use the "Full ingest"
    workflow's uploaded artifact, for a database with the complete history a
    backtest needs.
    """

    cutoff = date.today() - timedelta(days=365 * config.ingest.price_retention_years)
    with get_session(config) as session:
        prices_deleted = session.query(PriceDaily).filter(PriceDaily.date < cutoff).delete()
        fx_deleted = session.query(FxRate).filter(FxRate.date < cutoff).delete()

    engine = get_engine(config)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM"))

    return {"prices_deleted": prices_deleted, "fx_deleted": fx_deleted}


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
            _refresh_fundamentals(
                ticker_row.ticker,
                fundamentals_provider,
                full=True,
                source=config.ingest.fundamentals_provider,
                as_of=end,
            )

    prune_result = prune_old_prices(config)
    logger.info(
        "Pruned %d price bar(s) and %d FX rate(s) older than %d year(s)",
        prune_result["prices_deleted"],
        prune_result["fx_deleted"],
        config.ingest.price_retention_years,
    )


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
            _refresh_fundamentals(
                ticker_row.ticker,
                fundamentals_provider,
                full=False,
                source=config.ingest.fundamentals_provider,
                as_of=end,
            )


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
