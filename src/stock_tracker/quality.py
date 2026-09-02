"""Quality filter: scores each eligible-universe ticker on six hard criteria
and stores a dated snapshot so later modules (and the backtest) can look up
who was eligible as of any given date without look-ahead.

All queries here are point-in-time: fundamentals are filtered on
`report_date <= as_of` (the date the figures were actually filed, not the
fiscal period they cover), analyst estimates on `as_of_date <= as_of`, and
prices on `date <= as_of`. Nothing is read that wouldn't have been known on
`as_of` in real life.
"""

from __future__ import annotations

import argparse
import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from stock_tracker.config import Config, load_config
from stock_tracker.db.models import AnalystEstimate, Fundamentals, PriceDaily, QualityScore, Ticker
from stock_tracker.db.session import get_session, init_db
from stock_tracker.fx import convert_to_eur
from stock_tracker.logging_setup import setup_logging

logger = logging.getLogger("stock_tracker.quality")

_CRITERIA_COUNT = 6


@dataclass(frozen=True)
class QualityResult:
    ticker: str
    date: date
    market_cap_eur: float | None
    market_cap_pass: bool
    fcf_positive_pass: bool
    revenue_growth_ttm_pct: float | None
    revenue_growth_pass: bool
    net_debt_to_ebitda: float | None
    net_debt_ebitda_pass: bool
    eps_estimate_current: float | None
    eps_estimate_prior: float | None
    eps_estimate_trend_pass: bool
    avg_daily_volume: float | None
    volume_pass: bool

    @property
    def score(self) -> int:
        return sum(
            [
                self.market_cap_pass,
                self.fcf_positive_pass,
                self.revenue_growth_pass,
                self.net_debt_ebitda_pass,
                self.eps_estimate_trend_pass,
                self.volume_pass,
            ]
        )

    @property
    def eligible(self) -> bool:
        return self.score == _CRITERIA_COUNT


def _latest_estimate(
    session: Session, ticker: str, fiscal_year: int, on_or_before: date
) -> AnalystEstimate | None:
    return (
        session.query(AnalystEstimate)
        .filter(
            AnalystEstimate.ticker == ticker,
            AnalystEstimate.fiscal_year == fiscal_year,
            AnalystEstimate.as_of_date <= on_or_before,
        )
        .order_by(AnalystEstimate.as_of_date.desc())
        .first()
    )


def _average_volume(
    session: Session, ticker: str, as_of: date, lookback_trading_days: int
) -> float | None:
    rows = (
        session.query(PriceDaily.volume)
        .filter(PriceDaily.ticker == ticker, PriceDaily.date <= as_of)
        .order_by(PriceDaily.date.desc())
        .limit(lookback_trading_days)
        .all()
    )
    if not rows:
        return None
    volumes = [v for (v,) in rows]
    return sum(volumes) / len(volumes)


def compute_quality(
    session: Session, ticker_row: Ticker, as_of: date, config: Config
) -> QualityResult | None:
    """Computes one ticker's quality snapshot as of `as_of`. Returns None if
    there isn't even one fundamentals report yet (too early to score)."""

    quality_cfg = config.quality

    fundamentals = (
        session.query(Fundamentals)
        .filter(Fundamentals.ticker == ticker_row.ticker, Fundamentals.report_date <= as_of)
        .order_by(Fundamentals.period_end.desc())
        .limit(12)
        .all()
    )
    if not fundamentals:
        return None

    latest = fundamentals[0]
    last_4 = fundamentals[:4]

    market_cap_eur = None
    if latest.market_cap is not None:
        try:
            market_cap_eur = convert_to_eur(
                latest.market_cap, ticker_row.currency, latest.report_date
            )
        except LookupError:
            market_cap_eur = None
    market_cap_pass = market_cap_eur is not None and market_cap_eur > quality_cfg.min_market_cap_eur

    fcf_positive_pass = len(last_4) == 4 and all(
        q.free_cash_flow is not None and q.free_cash_flow > 0 for q in last_4
    )

    revenue_growth_ttm_pct = None
    if len(fundamentals) >= 8 and all(q.revenue is not None for q in fundamentals[:8]):
        ttm_now = sum(q.revenue for q in fundamentals[:4])
        ttm_prior = sum(q.revenue for q in fundamentals[4:8])
        if ttm_prior:
            revenue_growth_ttm_pct = (ttm_now / ttm_prior - 1) * 100
    revenue_growth_pass = (
        revenue_growth_ttm_pct is not None
        and revenue_growth_ttm_pct > quality_cfg.min_revenue_growth_ttm_pct
    )

    net_debt_to_ebitda = None
    has_ebitda = len(last_4) == 4 and all(q.ebitda is not None for q in last_4)
    if has_ebitda and latest.net_debt is not None:
        ttm_ebitda = sum(q.ebitda for q in last_4)
        if ttm_ebitda:
            net_debt_to_ebitda = latest.net_debt / ttm_ebitda
    net_debt_ebitda_pass = (
        net_debt_to_ebitda is not None and net_debt_to_ebitda < quality_cfg.max_net_debt_to_ebitda
    )

    next_fiscal_year = as_of.year + 1
    lookback_date = as_of - timedelta(days=quality_cfg.eps_estimate_lookback_days)
    current_estimate = _latest_estimate(session, ticker_row.ticker, next_fiscal_year, as_of)
    prior_estimate = _latest_estimate(session, ticker_row.ticker, next_fiscal_year, lookback_date)
    eps_estimate_current = current_estimate.eps_estimate if current_estimate else None
    eps_estimate_prior = prior_estimate.eps_estimate if prior_estimate else None
    eps_estimate_trend_pass = (
        eps_estimate_current is not None
        and eps_estimate_prior is not None
        and eps_estimate_current > eps_estimate_prior
    )

    avg_daily_volume = _average_volume(
        session, ticker_row.ticker, as_of, quality_cfg.avg_volume_lookback_days
    )
    volume_pass = (
        avg_daily_volume is not None and avg_daily_volume > quality_cfg.min_avg_daily_volume
    )

    return QualityResult(
        ticker=ticker_row.ticker,
        date=as_of,
        market_cap_eur=market_cap_eur,
        market_cap_pass=market_cap_pass,
        fcf_positive_pass=fcf_positive_pass,
        revenue_growth_ttm_pct=revenue_growth_ttm_pct,
        revenue_growth_pass=revenue_growth_pass,
        net_debt_to_ebitda=net_debt_to_ebitda,
        net_debt_ebitda_pass=net_debt_ebitda_pass,
        eps_estimate_current=eps_estimate_current,
        eps_estimate_prior=eps_estimate_prior,
        eps_estimate_trend_pass=eps_estimate_trend_pass,
        avg_daily_volume=avg_daily_volume,
        volume_pass=volume_pass,
    )


def compute_quality_for_universe(as_of: date, config: Config | None = None) -> list[QualityResult]:
    config = config or load_config()
    results = []
    with get_session() as session:
        tickers = session.query(Ticker).filter(Ticker.active.is_(True)).all()
        for ticker_row in tickers:
            result = compute_quality(session, ticker_row, as_of, config)
            if result is not None:
                results.append(result)
            else:
                logger.debug("%s: no fundamentals as of %s yet, skipping", ticker_row.ticker, as_of)
    return results


def store_quality_scores(results: list[QualityResult]) -> int:
    if not results:
        return 0
    with get_session() as session:
        stored = 0
        for r in results:
            existing = (
                session.query(QualityScore)
                .filter(QualityScore.ticker == r.ticker, QualityScore.date == r.date)
                .first()
            )
            values = {
                "score": r.score,
                "eligible": r.eligible,
                "market_cap_eur": r.market_cap_eur,
                "market_cap_pass": r.market_cap_pass,
                "fcf_positive_pass": r.fcf_positive_pass,
                "revenue_growth_ttm_pct": r.revenue_growth_ttm_pct,
                "revenue_growth_pass": r.revenue_growth_pass,
                "net_debt_to_ebitda": r.net_debt_to_ebitda,
                "net_debt_ebitda_pass": r.net_debt_ebitda_pass,
                "eps_estimate_current": r.eps_estimate_current,
                "eps_estimate_prior": r.eps_estimate_prior,
                "eps_estimate_trend_pass": r.eps_estimate_trend_pass,
                "avg_daily_volume": r.avg_daily_volume,
                "volume_pass": r.volume_pass,
            }
            if existing is not None:
                for key, value in values.items():
                    setattr(existing, key, value)
            else:
                session.add(QualityScore(ticker=r.ticker, date=r.date, **values))
            stored += 1
    return stored


def get_eligible_tickers(as_of: date) -> list[str]:
    """Returns eligible tickers using each ticker's most recently computed
    quality snapshot on or before `as_of` — quality only recomputes weekly,
    but callers (e.g. the signal engine) need an answer for every trading day."""

    with get_session() as session:
        rows = (
            session.query(QualityScore)
            .filter(QualityScore.date <= as_of)
            .order_by(QualityScore.ticker, QualityScore.date.desc())
            .all()
        )
    latest_by_ticker: dict[str, QualityScore] = {}
    for row in rows:
        latest_by_ticker.setdefault(row.ticker, row)
    return sorted(ticker for ticker, row in latest_by_ticker.items() if row.eligible)


def run_for_date(as_of: date, config: Config | None = None) -> int:
    config = config or load_config()
    logger.info("Computing quality scores as of %s", as_of)
    results = compute_quality_for_universe(as_of, config)
    stored = store_quality_scores(results)
    eligible_count = sum(1 for r in results if r.eligible)
    logger.info("%d/%d scored tickers eligible as of %s", eligible_count, len(results), as_of)
    return stored


def backfill(start: date, end: date, config: Config | None = None) -> int:
    """Recomputes quality scores for every occurrence of `quality.recompute_weekday`
    in [start, end]. Used to populate history after `ingest.py --full` so the
    backtest has point-in-time eligibility to work with."""

    config = config or load_config()
    target_weekday = list(calendar.day_name).index(config.quality.recompute_weekday)

    total = 0
    current = start
    while current <= end:
        if current.weekday() == target_weekday:
            total += run_for_date(current, config)
        current += timedelta(days=1)
    return total


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="stock-tracker quality filter")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="Compute for this date (default: today)",
    )
    parser.add_argument("--backfill-start", type=date.fromisoformat, default=None)
    parser.add_argument("--backfill-end", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)

    if bool(args.backfill_start) != bool(args.backfill_end):
        parser.error("--backfill-start and --backfill-end must be given together")

    config = load_config()
    setup_logging(config)
    init_db(config)

    if args.backfill_start and args.backfill_end:
        backfill(args.backfill_start, args.backfill_end, config)
    else:
        run_for_date(args.date or date.today(), config)


if __name__ == "__main__":
    main()
