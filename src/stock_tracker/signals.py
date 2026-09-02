"""Signal engine: entry signals (tranche 1/2/3) for eligible tickers and exit
signals (profit target, close-above-SMA50, time-stop REVIEW, fundamental
EXIT_FUNDAMENTAL) for open paper positions.

Like quality.py, everything is point-in-time — indicators are built only from
prices/estimates/earnings dates known as of `as_of`, so a signal computed for
a historical date matches what would have fired in real time.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from stock_tracker.config import Config, load_config
from stock_tracker.db.models import EarningsCalendar, PriceDaily, Signal, TradePaper
from stock_tracker.db.session import get_session, init_db
from stock_tracker.logging_setup import setup_logging
from stock_tracker.quality import get_eligible_tickers, latest_estimate

logger = logging.getLogger("stock_tracker.signals")

ENTRY = "ENTRY"
EXIT = "EXIT"
REVIEW = "REVIEW"
EXIT_FUNDAMENTAL = "EXIT_FUNDAMENTAL"


@dataclass(frozen=True)
class PriceIndicators:
    date: date
    close: float
    sma50: float | None
    rsi14: float | None
    high_52w: float | None
    distance_to_52w_high_pct: float | None


@dataclass(frozen=True)
class SignalResult:
    ticker: str
    date: date
    signal_type: str
    tranche: int | None
    price: float
    sma50: float | None
    rsi14: float | None
    distance_52w_high: float | None
    notes: str | None = None


def _sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _rsi(closes: list[float], period: int) -> float | None:
    """Simple (unsmoothed) RSI over `period` daily changes."""
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1) :]
    diffs = [curr - prev for prev, curr in zip(window, window[1:], strict=False)]
    gains = [max(diff, 0.0) for diff in diffs]
    losses = [max(-diff, 0.0) for diff in diffs]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_indicators(session, ticker: str, as_of: date, config: Config) -> PriceIndicators | None:
    """Returns SMA50/RSI14/52-week-high indicators as of `as_of`, or None if
    there's no price bar for `as_of` itself (e.g. weekend/holiday, or the
    ticker has no history yet)."""

    signals_cfg = config.signals
    sma_period = signals_cfg.sma_period
    rsi_period = signals_cfg.tranche_1.rsi_period
    high_lookback_days = signals_cfg.high_lookback_weeks * 5  # ~5 trading days/week

    lookback = max(sma_period, rsi_period + 1, high_lookback_days)
    rows = (
        session.query(PriceDaily)
        .filter(PriceDaily.ticker == ticker, PriceDaily.date <= as_of)
        .order_by(PriceDaily.date.desc())
        .limit(lookback)
        .all()
    )
    if not rows or rows[0].date != as_of:
        return None

    rows = list(reversed(rows))  # chronological order
    closes = [r.close for r in rows]
    today_close = closes[-1]

    high_52w = max(closes[-high_lookback_days:])
    distance = (today_close / high_52w - 1) * 100

    return PriceIndicators(
        date=as_of,
        close=today_close,
        sma50=_sma(closes, sma_period),
        rsi14=_rsi(closes, rsi_period),
        high_52w=high_52w,
        distance_to_52w_high_pct=distance,
    )


def _entry_tranche(indicators: PriceIndicators, config: Config) -> int | None:
    """Returns the deepest tranche whose price threshold is met, or None.
    Tranche 2/3 are price-only; tranche 1 additionally requires RSI(14) < threshold."""

    if indicators.sma50 is None:
        return None
    signals_cfg = config.signals
    close, sma50 = indicators.close, indicators.sma50

    if close <= (1 - signals_cfg.tranche_3.sma50_discount_pct / 100) * sma50:
        return 3
    if close <= (1 - signals_cfg.tranche_2.sma50_discount_pct / 100) * sma50:
        return 2
    if (
        close <= (1 - signals_cfg.tranche_1.sma50_discount_pct / 100) * sma50
        and indicators.rsi14 is not None
        and indicators.rsi14 < signals_cfg.tranche_1.rsi_threshold
    ):
        return 1
    return None


def _within_earnings_guard(session, ticker: str, as_of: date, guard_trading_days: int) -> bool:
    """True if an earnings date falls within the `guard_trading_days` trading
    days strictly *before* `as_of` (the guard doesn't cover the earnings day
    itself, only the run-up to it)."""

    upcoming = (
        session.query(EarningsCalendar)
        .filter(EarningsCalendar.ticker == ticker, EarningsCalendar.earnings_date >= as_of)
        .order_by(EarningsCalendar.earnings_date.asc())
        .first()
    )
    if upcoming is None:
        return False
    trading_days_until = int(np.busday_count(as_of, upcoming.earnings_date))
    return 1 <= trading_days_until <= guard_trading_days


def _eps_estimate_dropped(session, ticker: str, as_of: date, config: Config) -> bool:
    fiscal_year = as_of.year + 1
    lookback_date = as_of - timedelta(days=config.quality.eps_estimate_lookback_days)
    current = latest_estimate(session, ticker, fiscal_year, as_of)
    prior = latest_estimate(session, ticker, fiscal_year, lookback_date)
    if current is None or prior is None or prior.eps_estimate == 0:
        return False
    drop_pct = (1 - current.eps_estimate / prior.eps_estimate) * 100
    return drop_pct > config.signals.exit.eps_estimate_drop_pct


def generate_entry_signals(as_of: date, config: Config | None = None) -> list[SignalResult]:
    config = config or load_config()
    eligible = get_eligible_tickers(as_of)
    guard_days = config.signals.earnings_guard_days

    results = []
    with get_session() as session:
        for ticker in eligible:
            indicators = compute_indicators(session, ticker, as_of, config)
            if indicators is None:
                continue
            tranche = _entry_tranche(indicators, config)
            if tranche is None:
                continue
            if _within_earnings_guard(session, ticker, as_of, guard_days):
                logger.debug("%s: entry suppressed by earnings guard", ticker)
                continue
            results.append(
                SignalResult(
                    ticker=ticker,
                    date=as_of,
                    signal_type=ENTRY,
                    tranche=tranche,
                    price=indicators.close,
                    sma50=indicators.sma50,
                    rsi14=indicators.rsi14,
                    distance_52w_high=indicators.distance_to_52w_high_pct,
                )
            )
    return results


def generate_exit_signals(as_of: date, config: Config | None = None) -> list[SignalResult]:
    config = config or load_config()
    exit_cfg = config.signals.exit
    eligible = set(get_eligible_tickers(as_of))

    results: list[SignalResult] = []
    with get_session() as session:
        open_trades = session.query(TradePaper).filter(TradePaper.status == "OPEN").all()
        tickers_with_open_trades = sorted({t.ticker for t in open_trades})
        indicators_by_ticker = {
            ticker: compute_indicators(session, ticker, as_of, config)
            for ticker in tickers_with_open_trades
        }

        for trade in open_trades:
            indicators = indicators_by_ticker.get(trade.ticker)
            if indicators is None:
                continue

            return_pct = (indicators.close / trade.entry_price - 1) * 100
            hit_profit_target = return_pct >= exit_cfg.profit_target_pct
            closed_above_sma = indicators.sma50 is not None and indicators.close > indicators.sma50

            if hit_profit_target or closed_above_sma:
                results.append(
                    SignalResult(
                        ticker=trade.ticker,
                        date=as_of,
                        signal_type=EXIT,
                        tranche=trade.tranche,
                        price=indicators.close,
                        sma50=indicators.sma50,
                        rsi14=indicators.rsi14,
                        distance_52w_high=indicators.distance_to_52w_high_pct,
                        notes="profit_target" if hit_profit_target else "close_above_sma50",
                    )
                )
                continue

            weeks_open = (as_of - trade.entry_date).days / 7
            if weeks_open > exit_cfg.time_stop_weeks:
                results.append(
                    SignalResult(
                        ticker=trade.ticker,
                        date=as_of,
                        signal_type=REVIEW,
                        tranche=trade.tranche,
                        price=indicators.close,
                        sma50=indicators.sma50,
                        rsi14=indicators.rsi14,
                        distance_52w_high=indicators.distance_to_52w_high_pct,
                        notes=f"open {weeks_open:.1f} weeks without recovery",
                    )
                )

        # Fundamental stop is ticker-level (not per-tranche): one row covers
        # every open tranche of that ticker.
        for ticker in tickers_with_open_trades:
            indicators = indicators_by_ticker.get(ticker)
            if indicators is None:
                continue
            fell_out_of_eligible = ticker not in eligible
            eps_dropped = _eps_estimate_dropped(session, ticker, as_of, config)
            if not (fell_out_of_eligible or eps_dropped):
                continue
            reasons = []
            if fell_out_of_eligible:
                reasons.append("no longer eligible")
            if eps_dropped:
                reasons.append("eps estimate dropped")
            results.append(
                SignalResult(
                    ticker=ticker,
                    date=as_of,
                    signal_type=EXIT_FUNDAMENTAL,
                    tranche=None,
                    price=indicators.close,
                    sma50=indicators.sma50,
                    rsi14=indicators.rsi14,
                    distance_52w_high=indicators.distance_to_52w_high_pct,
                    notes=", ".join(reasons),
                )
            )

    return results


def store_signals(results: list[SignalResult]) -> int:
    if not results:
        return 0
    with get_session() as session:
        stored = 0
        for r in results:
            existing = (
                session.query(Signal)
                .filter(
                    Signal.ticker == r.ticker,
                    Signal.date == r.date,
                    Signal.signal_type == r.signal_type,
                    Signal.tranche == r.tranche,
                )
                .first()
            )
            if existing is not None:
                existing.price = r.price
                existing.sma50 = r.sma50
                existing.rsi14 = r.rsi14
                existing.distance_52w_high = r.distance_52w_high
                existing.notes = r.notes
            else:
                session.add(
                    Signal(
                        ticker=r.ticker,
                        date=r.date,
                        tranche=r.tranche,
                        signal_type=r.signal_type,
                        price=r.price,
                        sma50=r.sma50,
                        rsi14=r.rsi14,
                        distance_52w_high=r.distance_52w_high,
                        notes=r.notes,
                    )
                )
            stored += 1
    return stored


def run_for_date(as_of: date, config: Config | None = None) -> int:
    config = config or load_config()
    logger.info("Generating signals as of %s", as_of)
    entries = generate_entry_signals(as_of, config)
    exits = generate_exit_signals(as_of, config)
    stored = store_signals(entries + exits)
    logger.info(
        "%d entry, %d exit/review/fundamental signals as of %s", len(entries), len(exits), as_of
    )
    return stored


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="stock-tracker signal engine")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="Generate signals for this date (default: today)",
    )
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config)
    init_db(config)

    run_for_date(args.date or date.today(), config)


if __name__ == "__main__":
    main()
