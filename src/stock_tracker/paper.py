"""Paper trading: fills orders from stored signals using the next available
close price (no look-ahead), and reports realised/unrealised performance
against the backtest's expectations and the benchmark.

`trades_paper` is the only persisted state — there's no separate cash
ledger. Each run reconstructs the current portfolio (cash, open/closed
trades, tax already settled) by replaying `trades_paper` chronologically
through the same cost/sizing rules as Module 4's backtester
(`backtest.Portfolio`), so paper results stay directly comparable to the
backtest's out-of-sample numbers.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from stock_tracker import backtest, signals
from stock_tracker.config import Config, load_config
from stock_tracker.db.models import PriceDaily, Signal, Ticker, TradePaper
from stock_tracker.db.session import get_session, init_db
from stock_tracker.logging_setup import setup_logging

logger = logging.getLogger("stock_tracker.paper")


def _build_cost_sizing(config: Config) -> tuple[backtest.CostConfig, backtest.SizingConfig]:
    costs = backtest.CostConfig(
        slippage_pct=config.backtest.costs.slippage_pct,
        tob_pct=config.backtest.costs.belgian_tob_pct,
        broker_fee_pct=config.backtest.costs.broker_fee_pct,
        tax_pct=config.backtest.costs.capital_gains_tax_pct,
    )
    sizing = backtest.SizingConfig(
        max_pct_per_tranche=config.backtest.position_sizing.max_pct_per_tranche,
        max_pct_per_ticker=config.backtest.position_sizing.max_pct_per_ticker,
        max_pct_per_sector=config.backtest.position_sizing.max_pct_per_sector,
        min_cash_pct=config.backtest.position_sizing.min_cash_pct,
    )
    return costs, sizing


def reconstruct_portfolio(
    session, config: Config
) -> tuple[backtest.Portfolio, dict[int, TradePaper]]:
    """Rebuilds cash/open-trades/closed-trades/tax-ledger purely from
    `trades_paper`, replayed in entry-date order through the same cost model
    as the backtester. Returns the portfolio plus a mapping from each open
    `BacktestTrade` (by `id()`) back to its source `TradePaper` row, so a
    caller can update that row when the trade later closes."""

    costs, sizing = _build_cost_sizing(config)
    portfolio = backtest.Portfolio(
        cash=config.paper_trading.starting_capital_eur, sizing=sizing, costs=costs
    )
    sector_by_ticker = {t.ticker: (t.sector or "Unknown") for t in session.query(Ticker).all()}
    rows = session.query(TradePaper).order_by(TradePaper.entry_date).all()

    open_row_by_trade: dict[int, TradePaper] = {}
    for row in rows:
        entry_cost = row.size * costs.transaction_cost_pct / 100
        outlay = row.size + entry_cost
        portfolio.cash -= outlay
        trade = backtest.BacktestTrade(
            ticker=row.ticker,
            tranche=row.tranche,
            sector=sector_by_ticker.get(row.ticker, "Unknown"),
            entry_date=row.entry_date,
            entry_price=row.entry_price,
            size_eur=row.size,
            entry_cash_outlay=outlay,
        )
        if row.status == "CLOSED" and row.exit_price is not None:
            gross = trade.market_value(row.exit_price)
            exit_cost = gross * costs.transaction_cost_pct / 100
            net = gross - exit_cost
            portfolio.cash += net
            trade.exit_date = row.exit_date
            trade.exit_price = row.exit_price
            trade.status = "CLOSED"
            trade.realized_pnl_eur = net - outlay
            portfolio.closed_trades.append(trade)
            portfolio.realized_pnl_by_year[row.exit_date.year] = (
                portfolio.realized_pnl_by_year.get(row.exit_date.year, 0.0)
                + trade.realized_pnl_eur
            )
        else:
            portfolio.open_trades.append(trade)
            open_row_by_trade[id(trade)] = row

    current_year = date.today().year
    for year in sorted(portfolio.realized_pnl_by_year):
        if year < current_year:
            portfolio.settle_tax(year)

    return portfolio, open_row_by_trade


def fill_pending_signals(as_of: date, config: Config | None = None) -> dict:
    """Fills every signal older than `as_of` that hasn't been processed yet,
    using `as_of`'s close price — normally that's exactly "the next trading
    day's close" relative to the signal (the daily run processes yesterday's
    signals today). If a run is missed for several days, any backlog is
    filled at `as_of`'s price rather than each one's own next-day price —
    a deliberate simplification for a rare degraded case, not the common
    path."""

    config = config or load_config()
    opened = 0
    closed = 0

    with get_session() as session:
        portfolio, open_row_by_trade = reconstruct_portfolio(session, config)
        sector_by_ticker = {t.ticker: (t.sector or "Unknown") for t in session.query(Ticker).all()}
        price_cache: dict[str, float | None] = {}

        def price_lookup(ticker: str) -> float | None:
            if ticker not in price_cache:
                row = (
                    session.query(PriceDaily)
                    .filter(PriceDaily.ticker == ticker, PriceDaily.date == as_of)
                    .first()
                )
                price_cache[ticker] = row.close_eur if row else None
            return price_cache[ticker]

        pending = (
            session.query(Signal)
            .filter(Signal.processed.is_(False), Signal.date < as_of)
            .order_by(Signal.date, Signal.id)
            .all()
        )

        for sig in pending:
            price = price_lookup(sig.ticker)
            if price is None:
                # No price yet for as_of (holiday, or not ingested yet) —
                # leave unprocessed for a later run.
                continue
            sig.processed = True

            if sig.signal_type == signals.ENTRY:
                if portfolio.has_open_tranche(sig.ticker, sig.tranche):
                    continue
                equity = portfolio.equity(price_lookup)
                sector = sector_by_ticker.get(sig.ticker, "Unknown")
                target_size = equity * portfolio.sizing.max_pct_per_tranche / 100
                if not portfolio.can_enter(sig.ticker, sector, target_size, equity, price_lookup):
                    continue
                trade = portfolio.enter(sig.ticker, sig.tranche, sector, as_of, price, target_size)
                row = TradePaper(
                    ticker=trade.ticker,
                    tranche=trade.tranche,
                    entry_date=as_of,
                    entry_price=trade.entry_price,
                    size=trade.size_eur,
                    status="OPEN",
                )
                session.add(row)
                open_row_by_trade[id(trade)] = row
                opened += 1

            elif sig.signal_type in (signals.EXIT, signals.EXIT_FUNDAMENTAL):
                matches = [
                    t
                    for t in list(portfolio.open_trades)
                    if t.ticker == sig.ticker and (sig.tranche is None or t.tranche == sig.tranche)
                ]
                for trade in matches:
                    row = open_row_by_trade.get(id(trade))
                    portfolio.exit_trade(trade, as_of, price, sig.signal_type.lower())
                    if row is not None:
                        row.exit_date = as_of
                        row.exit_price = trade.exit_price
                        row.status = "CLOSED"
                        row.pnl = trade.realized_pnl_eur
                    closed += 1
            # REVIEW: informational only — no order, just marked processed above.

    logger.info("Paper trading %s: opened %d, closed %d", as_of, opened, closed)
    return {"date": as_of, "opened": opened, "closed": closed}


# --- reporting ---------------------------------------------------------------


def _period_return_pct(equity_curve: list[tuple[date, float]]) -> float | None:
    if not equity_curve:
        return None
    first_value = equity_curve[0][1]
    if first_value == 0:
        return None
    return (equity_curve[-1][1] / first_value - 1) * 100


def _implied_period_return_pct(cagr_pct: float, days: int) -> float:
    return ((1 + cagr_pct / 100) ** (days / 365.25) - 1) * 100


def monthly_report(month_start: date, month_end: date, config: Config | None = None) -> dict:
    config = config or load_config()

    with get_session() as session:
        closed_this_month = (
            session.query(TradePaper)
            .filter(
                TradePaper.status == "CLOSED",
                TradePaper.exit_date >= month_start,
                TradePaper.exit_date <= month_end,
            )
            .all()
        )
        open_trades = session.query(TradePaper).filter(TradePaper.status == "OPEN").all()

        realized_pnl_eur = sum(t.pnl or 0.0 for t in closed_this_month)

        unrealized_pnl_eur = 0.0
        for trade in open_trades:
            price_row = (
                session.query(PriceDaily)
                .filter(PriceDaily.ticker == trade.ticker, PriceDaily.date <= month_end)
                .order_by(PriceDaily.date.desc())
                .first()
            )
            if price_row:
                unrealized_pnl_eur += trade.size * (price_row.close_eur / trade.entry_price - 1)

    benchmark_result = backtest.simulate_benchmark(month_start, month_end, config)
    benchmark_return_pct = _period_return_pct(benchmark_result.equity_curve)

    runs = backtest.list_runs(limit=1)
    backtest_expected_return_pct = None
    if runs:
        out_metrics = runs[0]["metrics"].get("out_of_sample_strategy")
        if out_metrics is not None:
            days = (month_end - month_start).days + 1
            backtest_expected_return_pct = _implied_period_return_pct(out_metrics.cagr_pct, days)

    starting_capital = config.paper_trading.starting_capital_eur
    paper_return_pct = (
        (realized_pnl_eur + unrealized_pnl_eur) / starting_capital * 100
        if starting_capital
        else None
    )

    return {
        "month_start": month_start,
        "month_end": month_end,
        "realized_pnl_eur": realized_pnl_eur,
        "unrealized_pnl_eur": unrealized_pnl_eur,
        "num_closed_this_month": len(closed_this_month),
        "num_open": len(open_trades),
        "paper_return_pct": paper_return_pct,
        "backtest_expected_return_pct": backtest_expected_return_pct,
        "benchmark_return_pct": benchmark_return_pct,
    }


def compare_to_backtest(config: Config | None = None) -> dict | None:
    """Once at least `paper_trading.review_after_closed_trades` trades have
    closed, compares realised win rate and average profit-per-trade against
    the latest stored backtest's out-of-sample numbers. Returns None before
    that threshold or if no backtest run has been stored yet."""

    config = config or load_config()
    threshold = config.paper_trading.review_after_closed_trades

    with get_session() as session:
        closed = session.query(TradePaper).filter(TradePaper.status == "CLOSED").all()

    if len(closed) < threshold:
        return None

    wins = [t for t in closed if (t.pnl or 0) > 0]
    win_rate_pct = len(wins) / len(closed) * 100
    trade_returns = [(t.exit_price / t.entry_price - 1) * 100 for t in closed if t.exit_price]
    avg_profit_pct = sum(trade_returns) / len(trade_returns) if trade_returns else None

    runs = backtest.list_runs(limit=1)
    out_metrics = runs[0]["metrics"].get("out_of_sample_strategy") if runs else None

    within_win_rate_band = None
    within_avg_profit_band = None
    if out_metrics is not None:
        if out_metrics.win_rate_pct is not None:
            within_win_rate_band = (
                abs(win_rate_pct - out_metrics.win_rate_pct)
                <= config.paper_trading.win_rate_tolerance_pct
            )
        if out_metrics.avg_profit_per_trade_pct is not None and avg_profit_pct is not None:
            within_avg_profit_band = (
                abs(avg_profit_pct - out_metrics.avg_profit_per_trade_pct)
                <= config.paper_trading.avg_profit_tolerance_pct
            )

    return {
        "num_closed_trades": len(closed),
        "win_rate_pct": win_rate_pct,
        "avg_profit_pct": avg_profit_pct,
        "backtest_win_rate_pct": out_metrics.win_rate_pct if out_metrics else None,
        "backtest_avg_profit_pct": out_metrics.avg_profit_per_trade_pct if out_metrics else None,
        "within_win_rate_band": within_win_rate_band,
        "within_avg_profit_band": within_avg_profit_band,
    }


# --- CLI -----------------------------------------------------------------------


def _resolve_previous_month(today: date) -> tuple[date, date]:
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    return last_of_prev_month.replace(day=1), last_of_prev_month


def _resolve_month(month_str: str) -> tuple[date, date]:
    year_str, month_str = month_str.split("-")
    year, month = int(year_str), int(month_str)
    month_start = date(year, month, 1)
    next_month_start = date(year + (1 if month == 12 else 0), (month % 12) + 1, 1)
    return month_start, next_month_start - timedelta(days=1)


def print_monthly_report(report: dict) -> None:
    print(f"Paper trading report {report['month_start']} .. {report['month_end']}")
    print(
        f"  Realized P&L:   EUR {report['realized_pnl_eur']:.2f} "
        f"({report['num_closed_this_month']} trades closed)"
    )
    print(
        f"  Unrealized P&L: EUR {report['unrealized_pnl_eur']:.2f} ({report['num_open']} open)"
    )
    if report["paper_return_pct"] is not None:
        print(f"  Paper return:       {report['paper_return_pct']:.2f}%")
    if report["backtest_expected_return_pct"] is not None:
        print(f"  Backtest expected:  {report['backtest_expected_return_pct']:.2f}%")
    if report["benchmark_return_pct"] is not None:
        print(f"  Benchmark (S&P500): {report['benchmark_return_pct']:.2f}%")


def print_milestone(milestone: dict) -> None:
    print(f"Milestone: {milestone['num_closed_trades']} closed paper trades")
    print(
        f"  Win rate:   paper {milestone['win_rate_pct']:.1f}% vs "
        f"backtest {milestone['backtest_win_rate_pct']}"
    )
    print(
        f"  Avg profit: paper {milestone['avg_profit_pct']:.2f}% vs "
        f"backtest {milestone['backtest_avg_profit_pct']}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="stock-tracker paper trading")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="Fill pending signals as of this date (default: today)",
    )
    parser.add_argument("--monthly-report", action="store_true")
    parser.add_argument(
        "--month", type=str, default=None, help="YYYY-MM (default: previous calendar month)"
    )
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config)
    init_db(config)

    if args.monthly_report:
        month_start, month_end = (
            _resolve_month(args.month) if args.month else _resolve_previous_month(date.today())
        )
        print_monthly_report(monthly_report(month_start, month_end, config))
        return

    result = fill_pending_signals(args.date or date.today(), config)
    logger.info("Opened %d, closed %d paper trades", result["opened"], result["closed"])
    milestone = compare_to_backtest(config)
    if milestone:
        print_milestone(milestone)


if __name__ == "__main__":
    main()
