"""Walk-forward backtester: simulates the dip-buy strategy day by day over a
date range, applying position sizing, trading costs and yearly capital-gains
tax, then reports CAGR/drawdown/Sharpe/etc. against an S&P 500 buy & hold
benchmark. Every run is stored so different config versions can be compared.

Design note — pandas instead of vectorbt: the strategy's rules (per-tranche
sizing, per-ticker/per-sector exposure caps, a cash floor, yearly tax
settlement, point-in-time eligibility) are inherently sequential/stateful,
not a vectorized signal array. vectorbt's model fits cleanly vectorized
strategies; forcing this one into it would mean fighting the library more
than using it, on top of pulling in a heavy numba-compiled dependency this
sandbox doesn't need. So the simulation loop is plain Python (reusing the
same point-in-time building blocks as Module 2/3), and pandas is used where
it's a genuinely good fit: turning the resulting equity curve into
CAGR/drawdown/Sharpe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date

import numpy as np
import pandas as pd

from stock_tracker.config import Config, load_config
from stock_tracker.db.models import BacktestMetric, BacktestRun, PriceDaily, Ticker
from stock_tracker.db.session import get_session, init_db
from stock_tracker.logging_setup import setup_logging
from stock_tracker.quality import get_eligible_tickers
from stock_tracker.signals import eps_estimate_dropped, rsi, sma, within_earnings_guard
from stock_tracker.universe import BENCHMARK_TICKER

logger = logging.getLogger("stock_tracker.backtest")

TRADING_DAYS_PER_YEAR = 252

# --- indicators (EUR) -------------------------------------------------------
#
# signals.py's compute_indicators() works off native-currency `close` (what a
# trader watching the ticker on its home exchange would see). The backtest
# needs one consistent currency for money math (position sizing, costs, tax),
# so it recomputes SMA/RSI from `close_eur` instead — reusing the same pure
# sma()/rsi() math from signals.py, just fed a different price series.


@dataclass(frozen=True)
class EurIndicators:
    date: date
    close: float
    sma50: float | None
    rsi14: float | None


def indicators_eur(session, ticker: str, as_of: date, config: Config) -> EurIndicators | None:
    signals_cfg = config.signals
    sma_period = signals_cfg.sma_period
    rsi_period = signals_cfg.tranche_1.rsi_period
    lookback = max(sma_period, rsi_period + 1)

    rows = (
        session.query(PriceDaily)
        .filter(PriceDaily.ticker == ticker, PriceDaily.date <= as_of)
        .order_by(PriceDaily.date.desc())
        .limit(lookback)
        .all()
    )
    if not rows or rows[0].date != as_of:
        return None

    rows = list(reversed(rows))
    closes = [r.close_eur for r in rows]
    return EurIndicators(
        date=as_of,
        close=closes[-1],
        sma50=sma(closes, sma_period),
        rsi14=rsi(closes, rsi_period),
    )


def entry_tranche_eur(ind: EurIndicators, config: Config) -> int | None:
    """Same rule as signals.entry_tranche(), evaluated on EUR indicators."""

    if ind.sma50 is None:
        return None
    cfg = config.signals
    if ind.close <= (1 - cfg.tranche_3.sma50_discount_pct / 100) * ind.sma50:
        return 3
    if ind.close <= (1 - cfg.tranche_2.sma50_discount_pct / 100) * ind.sma50:
        return 2
    if (
        ind.close <= (1 - cfg.tranche_1.sma50_discount_pct / 100) * ind.sma50
        and ind.rsi14 is not None
        and ind.rsi14 < cfg.tranche_1.rsi_threshold
    ):
        return 1
    return None


class IndicatorCache:
    """Per-day memoization of indicators_eur() — every ticker is looked up at
    most once per simulated day, regardless of how many checks need it."""

    def __init__(self, session, as_of: date, config: Config):
        self._session = session
        self._as_of = as_of
        self._config = config
        self._cache: dict[str, EurIndicators | None] = {}

    def get(self, ticker: str) -> EurIndicators | None:
        if ticker not in self._cache:
            self._cache[ticker] = indicators_eur(self._session, ticker, self._as_of, self._config)
        return self._cache[ticker]


# --- portfolio / trade bookkeeping ------------------------------------------


@dataclass
class CostConfig:
    slippage_pct: float
    tob_pct: float
    broker_fee_pct: float
    tax_pct: float

    @property
    def transaction_cost_pct(self) -> float:
        return self.tob_pct + self.broker_fee_pct


@dataclass
class SizingConfig:
    max_pct_per_tranche: float
    max_pct_per_ticker: float
    max_pct_per_sector: float
    min_cash_pct: float


@dataclass
class BacktestTrade:
    ticker: str
    tranche: int
    sector: str
    entry_date: date
    entry_price: float  # EUR fill price, post-slippage
    size_eur: float  # notional invested at entry, excludes costs
    entry_cash_outlay: float  # size_eur + entry transaction costs
    status: str = "OPEN"
    exit_date: date | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    realized_pnl_eur: float | None = None

    def market_value(self, current_price_eur: float) -> float:
        return self.size_eur * (current_price_eur / self.entry_price)


PriceLookup = Callable[[str], float | None]


@dataclass
class Portfolio:
    cash: float
    sizing: SizingConfig
    costs: CostConfig
    open_trades: list[BacktestTrade] = field(default_factory=list)
    closed_trades: list[BacktestTrade] = field(default_factory=list)
    realized_pnl_by_year: dict[int, float] = field(default_factory=dict)
    tax_paid_by_year: dict[int, float] = field(default_factory=dict)

    def equity(self, price_lookup: PriceLookup) -> float:
        total = self.cash
        for trade in self.open_trades:
            price = price_lookup(trade.ticker)
            total += trade.market_value(price) if price is not None else trade.size_eur
        return total

    def _exposure(self, key: str, attr: str, price_lookup: PriceLookup) -> float:
        total = 0.0
        for trade in self.open_trades:
            if getattr(trade, attr) != key:
                continue
            price = price_lookup(trade.ticker)
            total += trade.market_value(price) if price is not None else trade.size_eur
        return total

    def has_open_tranche(self, ticker: str, tranche: int) -> bool:
        return any(t.ticker == ticker and t.tranche == tranche for t in self.open_trades)

    def can_enter(
        self, ticker: str, sector: str, target_size: float, equity: float, price_lookup: PriceLookup
    ) -> bool:
        tol = 1e-6
        if self._exposure(ticker, "ticker", price_lookup) + target_size > (
            equity * self.sizing.max_pct_per_ticker / 100 + tol
        ):
            return False
        if self._exposure(sector, "sector", price_lookup) + target_size > (
            equity * self.sizing.max_pct_per_sector / 100 + tol
        ):
            return False
        entry_cost = target_size * self.costs.transaction_cost_pct / 100
        cash_after = self.cash - (target_size + entry_cost)
        return cash_after >= equity * self.sizing.min_cash_pct / 100 - tol

    def enter(
        self,
        ticker: str,
        tranche: int,
        sector: str,
        as_of: date,
        close_eur: float,
        target_size: float,
    ) -> BacktestTrade:
        fill_price = close_eur * (1 + self.costs.slippage_pct / 100)
        cost = target_size * self.costs.transaction_cost_pct / 100
        outlay = target_size + cost
        self.cash -= outlay
        trade = BacktestTrade(
            ticker=ticker,
            tranche=tranche,
            sector=sector,
            entry_date=as_of,
            entry_price=fill_price,
            size_eur=target_size,
            entry_cash_outlay=outlay,
        )
        self.open_trades.append(trade)
        return trade

    def exit_trade(self, trade: BacktestTrade, as_of: date, close_eur: float, reason: str) -> None:
        fill_price = close_eur * (1 - self.costs.slippage_pct / 100)
        gross_proceeds = trade.market_value(fill_price)
        cost = gross_proceeds * self.costs.transaction_cost_pct / 100
        net_proceeds = gross_proceeds - cost
        self.cash += net_proceeds

        trade.exit_date = as_of
        trade.exit_price = fill_price
        trade.exit_reason = reason
        trade.status = "CLOSED"
        trade.realized_pnl_eur = net_proceeds - trade.entry_cash_outlay

        self.open_trades.remove(trade)
        self.closed_trades.append(trade)
        self.realized_pnl_by_year[as_of.year] = (
            self.realized_pnl_by_year.get(as_of.year, 0.0) + trade.realized_pnl_eur
        )

    def settle_tax(self, year: int) -> None:
        net = self.realized_pnl_by_year.get(year, 0.0)
        if net > 0:
            tax = net * self.costs.tax_pct / 100
            self.cash -= tax
            self.tax_paid_by_year[year] = tax


# --- metrics -----------------------------------------------------------------


@dataclass(frozen=True)
class Metrics:
    start: date
    end: date
    cagr_pct: float
    max_drawdown_pct: float
    longest_underwater_days: int
    win_rate_pct: float | None
    avg_holding_period_days: float | None
    num_trades: int | None
    sharpe: float
    final_equity_eur: float


def _longest_underwater_days(equity: pd.Series) -> int:
    running_max = equity.cummax()
    underwater = equity < running_max
    longest = 0
    start_of_streak = None
    for dt, is_underwater in underwater.items():
        if is_underwater:
            if start_of_streak is None:
                start_of_streak = dt
        elif start_of_streak is not None:
            longest = max(longest, (dt - start_of_streak).days)
            start_of_streak = None
    if start_of_streak is not None:
        longest = max(longest, (equity.index[-1] - start_of_streak).days)
    return longest


def compute_metrics(
    equity_curve: list[tuple[date, float]],
    closed_trades: list[BacktestTrade],
    risk_free_rate_pct: float,
    start: date,
    end: date,
) -> Metrics:
    if not equity_curve:
        return Metrics(start, end, 0.0, 0.0, 0, None, None, 0, 0.0, 0.0)

    dates, values = zip(*equity_curve, strict=True)
    equity = pd.Series(values, index=pd.DatetimeIndex(dates))

    years = (end - start).days / 365.25
    cagr_pct = (
        ((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1) * 100
        if years > 0 and equity.iloc[0] > 0
        else 0.0
    )

    drawdown = equity / equity.cummax() - 1
    max_drawdown_pct = float(drawdown.min()) * 100
    longest_underwater = _longest_underwater_days(equity)

    daily_returns = equity.pct_change().dropna()
    daily_rf = risk_free_rate_pct / 100 / TRADING_DAYS_PER_YEAR
    excess = daily_returns - daily_rf
    sharpe = (
        float(excess.mean() / excess.std() * np.sqrt(TRADING_DAYS_PER_YEAR))
        if len(excess) > 1 and excess.std() > 0
        else 0.0
    )

    num_trades = len(closed_trades) if closed_trades or equity_curve else None
    win_rate_pct = None
    avg_holding_period_days = None
    if closed_trades:
        wins = [t for t in closed_trades if (t.realized_pnl_eur or 0) > 0]
        win_rate_pct = len(wins) / len(closed_trades) * 100
        holding_periods = [(t.exit_date - t.entry_date).days for t in closed_trades]
        avg_holding_period_days = sum(holding_periods) / len(holding_periods)

    return Metrics(
        start=start,
        end=end,
        cagr_pct=cagr_pct,
        max_drawdown_pct=max_drawdown_pct,
        longest_underwater_days=longest_underwater,
        win_rate_pct=win_rate_pct,
        avg_holding_period_days=avg_holding_period_days,
        num_trades=num_trades,
        sharpe=sharpe,
        final_equity_eur=float(equity.iloc[-1]),
    )


# --- simulation ---------------------------------------------------------------


@dataclass(frozen=True)
class SimulationResult:
    equity_curve: list[tuple[date, float]]
    closed_trades: list[BacktestTrade]
    review_flag_count: int
    metrics: Metrics


def simulate(start: date, end: date, config: Config) -> SimulationResult:
    sizing = SizingConfig(
        max_pct_per_tranche=config.backtest.position_sizing.max_pct_per_tranche,
        max_pct_per_ticker=config.backtest.position_sizing.max_pct_per_ticker,
        max_pct_per_sector=config.backtest.position_sizing.max_pct_per_sector,
        min_cash_pct=config.backtest.position_sizing.min_cash_pct,
    )
    costs = CostConfig(
        slippage_pct=config.backtest.costs.slippage_pct,
        tob_pct=config.backtest.costs.belgian_tob_pct,
        broker_fee_pct=config.backtest.costs.broker_fee_pct,
        tax_pct=config.backtest.costs.capital_gains_tax_pct,
    )
    portfolio = Portfolio(cash=config.backtest.initial_capital_eur, sizing=sizing, costs=costs)

    equity_curve: list[tuple[date, float]] = []
    review_flag_count = 0
    current_year: int | None = None

    with get_session() as session:
        trading_days = [
            d
            for (d,) in session.query(PriceDaily.date)
            .filter(PriceDaily.date >= start, PriceDaily.date <= end)
            .distinct()
            .order_by(PriceDaily.date)
            .all()
        ]
        sector_by_ticker = {t.ticker: (t.sector or "Unknown") for t in session.query(Ticker).all()}

        for as_of in trading_days:
            if current_year is not None and as_of.year != current_year:
                portfolio.settle_tax(current_year)
            current_year = as_of.year

            cache = IndicatorCache(session, as_of, config)

            def price_lookup(ticker: str, _cache: IndicatorCache = cache) -> float | None:
                ind = _cache.get(ticker)
                return ind.close if ind else None

            eligible_today = set(get_eligible_tickers(as_of))

            # --- exits: recovered (profit target / close above SMA50) first,
            # then fundamental stop, then a REVIEW flag (informational only —
            # it does not close the position) ---
            for trade in list(portfolio.open_trades):
                ind = cache.get(trade.ticker)
                if ind is None:
                    continue
                return_pct = (ind.close / trade.entry_price - 1) * 100
                hit_profit = return_pct >= config.signals.exit.profit_target_pct
                closed_above_sma = ind.sma50 is not None and ind.close > ind.sma50
                if hit_profit or closed_above_sma:
                    reason = "profit_target" if hit_profit else "close_above_sma50"
                    portfolio.exit_trade(trade, as_of, ind.close, reason)
                    continue

                fell_out = trade.ticker not in eligible_today
                dropped = eps_estimate_dropped(session, trade.ticker, as_of, config)
                if fell_out or dropped:
                    portfolio.exit_trade(trade, as_of, ind.close, "fundamental_stop")
                    continue

                weeks_open = (as_of - trade.entry_date).days / 7
                if weeks_open > config.signals.exit.time_stop_weeks:
                    review_flag_count += 1

            # --- entries ---
            equity = portfolio.equity(price_lookup)
            for ticker in sorted(eligible_today):
                ind = cache.get(ticker)
                if ind is None:
                    continue
                tranche = entry_tranche_eur(ind, config)
                if tranche is None:
                    continue
                if portfolio.has_open_tranche(ticker, tranche):
                    # Already hold this exact tranche — don't re-buy it every
                    # day the price stays below its threshold; a deeper
                    # tranche (or a fresh entry after this one exits) is a
                    # separate, later opportunity.
                    continue
                guard_days = config.signals.earnings_guard_days
                if within_earnings_guard(session, ticker, as_of, guard_days):
                    continue
                target_size = equity * sizing.max_pct_per_tranche / 100
                sector = sector_by_ticker.get(ticker, "Unknown")
                if not portfolio.can_enter(ticker, sector, target_size, equity, price_lookup):
                    continue
                portfolio.enter(ticker, tranche, sector, as_of, ind.close, target_size)

            equity_curve.append((as_of, portfolio.equity(price_lookup)))

        if current_year is not None:
            portfolio.settle_tax(current_year)

    metrics = compute_metrics(
        equity_curve, portfolio.closed_trades, config.backtest.risk_free_rate_pct, start, end
    )
    return SimulationResult(
        equity_curve=equity_curve,
        closed_trades=portfolio.closed_trades,
        review_flag_count=review_flag_count,
        metrics=metrics,
    )


def simulate_benchmark(start: date, end: date, config: Config) -> SimulationResult:
    """Buy & hold on the benchmark index, starting with the same capital."""

    with get_session() as session:
        rows = (
            session.query(PriceDaily)
            .filter(
                PriceDaily.ticker == BENCHMARK_TICKER,
                PriceDaily.date >= start,
                PriceDaily.date <= end,
            )
            .order_by(PriceDaily.date)
            .all()
        )

    if not rows:
        logger.warning("No benchmark (%s) price data in [%s, %s]", BENCHMARK_TICKER, start, end)
        empty_metrics = Metrics(start, end, 0.0, 0.0, 0, None, None, None, 0.0, 0.0)
        return SimulationResult(
            equity_curve=[], closed_trades=[], review_flag_count=0, metrics=empty_metrics
        )

    initial_capital = config.backtest.initial_capital_eur
    base_price = rows[0].close_eur
    equity_curve = [(r.date, initial_capital * (r.close_eur / base_price)) for r in rows]

    metrics = compute_metrics(equity_curve, [], config.backtest.risk_free_rate_pct, start, end)
    # Buy & hold isn't a sequence of discrete trades — null out trade-specific fields.
    metrics = replace(metrics, win_rate_pct=None, avg_holding_period_days=None, num_trades=None)
    return SimulationResult(
        equity_curve=equity_curve, closed_trades=[], review_flag_count=0, metrics=metrics
    )


# --- overfitting check ---------------------------------------------------------


def check_overfitting(in_metrics: Metrics, out_metrics: Metrics, config: Config) -> str | None:
    cfg = config.backtest.overfitting_check
    reasons = []

    if in_metrics.cagr_pct > 0:
        degraded_threshold = in_metrics.cagr_pct * (1 - cfg.cagr_degradation_pct / 100)
        if out_metrics.cagr_pct < degraded_threshold:
            reasons.append(
                f"out-of-sample CAGR ({out_metrics.cagr_pct:.1f}%) is more than "
                f"{cfg.cagr_degradation_pct:.0f}% lower than in-sample ({in_metrics.cagr_pct:.1f}%)"
            )

    in_sample_healthy = in_metrics.sharpe > cfg.in_sample_sharpe_threshold
    out_of_sample_broke_down = out_metrics.sharpe <= cfg.sharpe_floor
    if in_sample_healthy and out_of_sample_broke_down:
        reasons.append(
            f"out-of-sample Sharpe ({out_metrics.sharpe:.2f}) dropped to/below "
            f"{cfg.sharpe_floor:.2f} while in-sample Sharpe was {in_metrics.sharpe:.2f}"
        )

    if not reasons:
        return None
    return "Possible overfitting: " + "; ".join(reasons)


# --- persistence ---------------------------------------------------------------


def config_hash(config: Config) -> str:
    payload = json.dumps(config.to_dict(), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def store_run(
    config: Config,
    in_sample: SimulationResult,
    out_of_sample: SimulationResult,
    benchmark_in_sample: SimulationResult,
    benchmark_out_of_sample: SimulationResult,
    overfitting_warning: str | None,
) -> int:
    with get_session() as session:
        run = BacktestRun(
            config_hash=config_hash(config),
            notes=overfitting_warning,
        )
        session.add(run)
        session.flush()  # populate run.id

        for period, result, source in (
            ("in_sample", in_sample, "strategy"),
            ("in_sample", benchmark_in_sample, "benchmark"),
            ("out_of_sample", out_of_sample, "strategy"),
            ("out_of_sample", benchmark_out_of_sample, "benchmark"),
        ):
            m = result.metrics
            session.add(
                BacktestMetric(
                    run_id=run.id,
                    period=period,
                    source=source,
                    start_date=m.start,
                    end_date=m.end,
                    cagr_pct=m.cagr_pct,
                    max_drawdown_pct=m.max_drawdown_pct,
                    longest_underwater_days=m.longest_underwater_days,
                    win_rate_pct=m.win_rate_pct,
                    avg_holding_period_days=m.avg_holding_period_days,
                    num_trades=m.num_trades,
                    sharpe=m.sharpe,
                    final_equity_eur=m.final_equity_eur,
                )
            )
        run_id = run.id
    return run_id


def list_runs(limit: int = 20) -> list[dict]:
    """Returns recent runs newest-first, each with its metrics keyed by
    '{period}_{source}' (e.g. 'out_of_sample_strategy') for easy comparison."""

    with get_session() as session:
        runs = (
            session.query(BacktestRun).order_by(BacktestRun.run_at.desc()).limit(limit).all()
        )
        results = []
        for run in runs:
            metrics_rows = (
                session.query(BacktestMetric).filter(BacktestMetric.run_id == run.id).all()
            )
            results.append(
                {
                    "id": run.id,
                    "run_at": run.run_at,
                    "config_hash": run.config_hash,
                    "notes": run.notes,
                    "metrics": {f"{m.period}_{m.source}": m for m in metrics_rows},
                }
            )
    return results


# --- orchestration / CLI --------------------------------------------------------


def _resolve_period_end(session, configured_end: str | None) -> date:
    if configured_end:
        return date.fromisoformat(configured_end)
    latest = session.query(PriceDaily.date).order_by(PriceDaily.date.desc()).first()
    if latest is None:
        raise RuntimeError("No price data ingested yet — run ingest.py --full first")
    return min(date.today(), latest[0])


def run(
    config: Config | None = None,
    in_start: date | None = None,
    in_end: date | None = None,
    out_start: date | None = None,
    out_end: date | None = None,
) -> dict:
    config = config or load_config()
    bt_cfg = config.backtest

    with get_session() as session:
        in_start = in_start or date.fromisoformat(bt_cfg.in_sample_start)
        in_end = in_end or date.fromisoformat(bt_cfg.in_sample_end)
        out_start = out_start or date.fromisoformat(bt_cfg.out_of_sample_start)
        out_end = out_end or _resolve_period_end(session, bt_cfg.get("out_of_sample_end"))

    logger.info("Running in-sample backtest %s..%s", in_start, in_end)
    in_sample = simulate(in_start, in_end, config)
    logger.info("Running out-of-sample backtest %s..%s", out_start, out_end)
    out_of_sample = simulate(out_start, out_end, config)

    benchmark_in_sample = simulate_benchmark(in_start, in_end, config)
    benchmark_out_of_sample = simulate_benchmark(out_start, out_end, config)

    warning = check_overfitting(in_sample.metrics, out_of_sample.metrics, config)
    if warning:
        logger.warning(warning)

    run_id = store_run(
        config, in_sample, out_of_sample, benchmark_in_sample, benchmark_out_of_sample, warning
    )

    return {
        "run_id": run_id,
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "benchmark_in_sample": benchmark_in_sample,
        "benchmark_out_of_sample": benchmark_out_of_sample,
        "overfitting_warning": warning,
    }


def _format_metrics_row(label: str, m: Metrics) -> str:
    win_rate = f"{m.win_rate_pct:.1f}%" if m.win_rate_pct is not None else "n/a"
    holding = (
        f"{m.avg_holding_period_days:.0f}d" if m.avg_holding_period_days is not None else "n/a"
    )
    trades = m.num_trades if m.num_trades is not None else "n/a"
    return (
        f"{label:<28} CAGR {m.cagr_pct:>7.2f}%  MaxDD {m.max_drawdown_pct:>7.2f}%  "
        f"UnderwaterDays {m.longest_underwater_days:>5}  Sharpe {m.sharpe:>6.2f}  "
        f"WinRate {win_rate:>7}  AvgHold {holding:>6}  Trades {trades!s:>6}"
    )


def print_report(result: dict) -> None:
    print(f"Backtest run #{result['run_id']}")
    print(_format_metrics_row("In-sample strategy", result["in_sample"].metrics))
    print(_format_metrics_row("In-sample benchmark", result["benchmark_in_sample"].metrics))
    print(_format_metrics_row("Out-of-sample strategy", result["out_of_sample"].metrics))
    print(_format_metrics_row("Out-of-sample benchmark", result["benchmark_out_of_sample"].metrics))
    if result["overfitting_warning"]:
        print(f"\n/!\\ {result['overfitting_warning']}")


def _print_runs(runs: list[dict]) -> None:
    for run_row in runs:
        print(f"#{run_row['id']}  {run_row['run_at']}  config={run_row['config_hash']}")
        for key, m in run_row["metrics"].items():
            print(f"    {key:<24} CAGR {m.cagr_pct:>7.2f}%  Sharpe {m.sharpe:>6.2f}")
        if run_row["notes"]:
            print(f"    note: {run_row['notes']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="stock-tracker walk-forward backtester")
    parser.add_argument("--in-start", type=date.fromisoformat, default=None)
    parser.add_argument("--in-end", type=date.fromisoformat, default=None)
    parser.add_argument("--out-start", type=date.fromisoformat, default=None)
    parser.add_argument("--out-end", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--list-runs", action="store_true", help="List past runs instead of running"
    )
    parser.add_argument(
        "--limit", type=int, default=10, help="Number of runs to show with --list-runs"
    )
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config)
    init_db(config)

    if args.list_runs:
        _print_runs(list_runs(args.limit))
        return

    result = run(
        config,
        in_start=args.in_start,
        in_end=args.in_end,
        out_start=args.out_start,
        out_end=args.out_end,
    )
    print_report(result)


if __name__ == "__main__":
    main()
