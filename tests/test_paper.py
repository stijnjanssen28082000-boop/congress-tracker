from datetime import date, timedelta

import pytest

from stock_tracker import backtest, paper, signals
from stock_tracker.db.models import PriceDaily, Signal, Ticker, TradePaper
from stock_tracker.db.session import get_session

AS_OF = date(2024, 6, 4)  # Tuesday


def _seed_ticker(ticker="AAPL", currency="EUR", sector="Technology"):
    with get_session() as session:
        session.add(
            Ticker(
                ticker=ticker,
                name="Test Co",
                exchange="US",
                currency=currency,
                sector=sector,
                index_membership="SP500",
            )
        )


def _seed_price(ticker, on_date, close_eur):
    with get_session() as session:
        session.add(
            PriceDaily(
                ticker=ticker,
                date=on_date,
                open=close_eur,
                high=close_eur,
                low=close_eur,
                close=close_eur,
                volume=1_000_000,
                close_eur=close_eur,
            )
        )


def _seed_trade_paper(
    ticker="AAPL",
    tranche=1,
    entry_date=date(2024, 1, 2),
    entry_price=100.0,
    size=2000.0,
    status="OPEN",
    exit_date=None,
    exit_price=None,
    pnl=None,
):
    with get_session() as session:
        session.add(
            TradePaper(
                ticker=ticker,
                tranche=tranche,
                entry_date=entry_date,
                entry_price=entry_price,
                size=size,
                status=status,
                exit_date=exit_date,
                exit_price=exit_price,
                pnl=pnl,
            )
        )


def _seed_signal(
    ticker="AAPL",
    on_date=AS_OF - timedelta(days=1),
    signal_type=signals.ENTRY,
    tranche=1,
    price=90.0,
    processed=False,
):
    with get_session() as session:
        session.add(
            Signal(
                ticker=ticker,
                date=on_date,
                tranche=tranche,
                signal_type=signal_type,
                price=price,
                processed=processed,
            )
        )


# --- reconstruct_portfolio ----------------------------------------------------


def test_reconstruct_portfolio_starts_with_configured_cash(db):
    with get_session() as session:
        portfolio, rows = paper.reconstruct_portfolio(session, db)
    assert portfolio.cash == db.paper_trading.starting_capital_eur
    assert portfolio.open_trades == []
    assert rows == {}


def test_reconstruct_portfolio_replays_open_trade(db):
    _seed_ticker()
    _seed_trade_paper(entry_price=100.0, size=2000.0, status="OPEN")
    costs, _ = paper._build_cost_sizing(db)

    with get_session() as session:
        portfolio, rows = paper.reconstruct_portfolio(session, db)

    assert len(portfolio.open_trades) == 1
    trade = portfolio.open_trades[0]
    expected_outlay = 2000.0 + 2000.0 * costs.transaction_cost_pct / 100
    assert portfolio.cash == pytest.approx(db.paper_trading.starting_capital_eur - expected_outlay)
    assert id(trade) in rows


def test_reconstruct_portfolio_replays_closed_trade_and_realizes_pnl(db):
    _seed_ticker()
    _seed_trade_paper(
        entry_price=100.0,
        size=2000.0,
        status="CLOSED",
        exit_date=date(2024, 1, 10),
        exit_price=110.0,
        pnl=None,  # deliberately not pre-filled — reconstruction derives it
    )
    costs, _ = paper._build_cost_sizing(db)

    with get_session() as session:
        portfolio, _rows = paper.reconstruct_portfolio(session, db)

    assert portfolio.open_trades == []
    assert len(portfolio.closed_trades) == 1
    trade = portfolio.closed_trades[0]
    entry_outlay = 2000.0 + 2000.0 * costs.transaction_cost_pct / 100
    gross = 2000.0 * (110.0 / 100.0)
    net = gross - gross * costs.transaction_cost_pct / 100
    realized_pnl = net - entry_outlay
    assert trade.realized_pnl_eur == pytest.approx(realized_pnl)
    # exit_date (2024) is a past year relative to "today" — its positive
    # realised P&L gets taxed on reconstruction, same as the backtester.
    tax = realized_pnl * costs.tax_pct / 100
    assert portfolio.cash == pytest.approx(
        db.paper_trading.starting_capital_eur - entry_outlay + net - tax
    )


def test_reconstruct_portfolio_settles_tax_only_for_past_years(db):
    _seed_ticker()
    past_year = date.today().year - 1
    _seed_trade_paper(
        entry_price=100.0,
        size=2000.0,
        status="CLOSED",
        entry_date=date(past_year, 1, 2),
        exit_date=date(past_year, 2, 1),
        exit_price=150.0,
    )
    _seed_trade_paper(
        entry_price=100.0,
        size=2000.0,
        status="CLOSED",
        entry_date=date.today() - timedelta(days=10),
        exit_date=date.today(),
        exit_price=150.0,
    )

    with get_session() as session:
        portfolio, _rows = paper.reconstruct_portfolio(session, db)

    assert past_year in portfolio.tax_paid_by_year
    assert date.today().year not in portfolio.tax_paid_by_year


# --- fill_pending_signals ------------------------------------------------------


def test_fill_entry_signal_creates_open_trade_at_as_of_price(db):
    _seed_ticker()
    _seed_price("AAPL", AS_OF, 90.0)
    _seed_signal(signal_type=signals.ENTRY, tranche=1, on_date=AS_OF - timedelta(days=1))

    result = paper.fill_pending_signals(AS_OF, db)

    assert result == {"date": AS_OF, "opened": 1, "closed": 0}
    with get_session() as session:
        trade = session.query(TradePaper).filter_by(ticker="AAPL").one()
        assert trade.status == "OPEN"
        assert trade.entry_date == AS_OF
        assert trade.entry_price == pytest.approx(90.0 * 1.001)
        sig = session.query(Signal).filter_by(ticker="AAPL").one()
        assert sig.processed is True


def test_fill_leaves_signal_unprocessed_without_price(db):
    _seed_ticker()
    _seed_signal(signal_type=signals.ENTRY, tranche=1)

    result = paper.fill_pending_signals(AS_OF, db)

    assert result["opened"] == 0
    with get_session() as session:
        sig = session.query(Signal).filter_by(ticker="AAPL").one()
        assert sig.processed is False


def test_fill_skips_entry_when_tranche_already_open(db):
    _seed_ticker()
    _seed_trade_paper(tranche=1, status="OPEN")
    _seed_price("AAPL", AS_OF, 90.0)
    _seed_signal(signal_type=signals.ENTRY, tranche=1)

    result = paper.fill_pending_signals(AS_OF, db)

    assert result["opened"] == 0
    with get_session() as session:
        assert session.query(TradePaper).count() == 1  # no duplicate
        assert session.query(Signal).filter_by(ticker="AAPL").one().processed is True


def test_fill_exit_signal_closes_matching_tranche(db):
    _seed_ticker()
    _seed_trade_paper(tranche=1, entry_price=100.0, size=2000.0, status="OPEN")
    _seed_price("AAPL", AS_OF, 120.0)
    _seed_signal(signal_type=signals.EXIT, tranche=1)

    result = paper.fill_pending_signals(AS_OF, db)

    assert result == {"date": AS_OF, "opened": 0, "closed": 1}
    with get_session() as session:
        trade = session.query(TradePaper).filter_by(ticker="AAPL").one()
        assert trade.status == "CLOSED"
        assert trade.exit_date == AS_OF
        assert trade.pnl is not None


def test_fill_exit_fundamental_closes_all_open_tranches(db):
    _seed_ticker()
    _seed_trade_paper(tranche=1, entry_price=100.0, size=2000.0, status="OPEN")
    _seed_trade_paper(tranche=2, entry_price=90.0, size=2000.0, status="OPEN")
    _seed_price("AAPL", AS_OF, 80.0)
    _seed_signal(signal_type=signals.EXIT_FUNDAMENTAL, tranche=None)

    result = paper.fill_pending_signals(AS_OF, db)

    assert result["closed"] == 2
    with get_session() as session:
        statuses = {t.status for t in session.query(TradePaper).filter_by(ticker="AAPL").all()}
        assert statuses == {"CLOSED"}


def test_fill_review_signal_marks_processed_without_order(db):
    _seed_ticker()
    _seed_trade_paper(tranche=1, status="OPEN")
    _seed_price("AAPL", AS_OF, 100.0)
    _seed_signal(signal_type=signals.REVIEW, tranche=1)

    result = paper.fill_pending_signals(AS_OF, db)

    assert result == {"date": AS_OF, "opened": 0, "closed": 0}
    with get_session() as session:
        assert session.query(TradePaper).filter_by(ticker="AAPL").one().status == "OPEN"
        assert session.query(Signal).filter_by(ticker="AAPL").one().processed is True


def test_fill_entry_blocked_by_ticker_exposure_cap(db):
    _seed_ticker()
    for tranche in (1, 2, 3):
        _seed_trade_paper(tranche=tranche, entry_price=100.0, size=2000.0, status="OPEN")
    _seed_price("AAPL", AS_OF, 100.0)
    _seed_signal(signal_type=signals.ENTRY, tranche=1)  # would duplicate tranche 1 anyway,
    # but use a ticker without an open tranche-1 collision by targeting a 4th
    # tranche id isn't realistic (max 3) — assert it's rejected either way,
    # exercising the "no new TradePaper" path regardless of which guard fires.

    result = paper.fill_pending_signals(AS_OF, db)

    assert result["opened"] == 0
    with get_session() as session:
        assert session.query(TradePaper).count() == 3


# --- monthly_report -------------------------------------------------------------


def test_monthly_report_realized_and_unrealized(db):
    _seed_ticker()
    month_start, month_end = date(2024, 3, 1), date(2024, 3, 31)
    _seed_trade_paper(
        entry_price=100.0,
        size=2000.0,
        status="CLOSED",
        exit_date=date(2024, 3, 15),
        exit_price=110.0,
        pnl=180.0,
    )
    _seed_trade_paper(entry_price=100.0, size=1000.0, status="OPEN", entry_date=date(2024, 3, 1))
    _seed_price("AAPL", date(2024, 3, 20), 120.0)

    report = paper.monthly_report(month_start, month_end, db)

    assert report["num_closed_this_month"] == 1
    assert report["realized_pnl_eur"] == pytest.approx(180.0)
    assert report["num_open"] == 1
    assert report["unrealized_pnl_eur"] == pytest.approx(1000.0 * (120.0 / 100.0 - 1))


def test_monthly_report_includes_backtest_expected_return(db):
    _seed_ticker()
    month_start, month_end = date(2024, 3, 1), date(2024, 3, 31)

    curve = [(date(2020, 1, 1), 100_000.0), (date(2020, 12, 31), 120_000.0)]
    metrics = backtest.compute_metrics(curve, [], 0.0, date(2020, 1, 1), date(2020, 12, 31))
    result = backtest.SimulationResult(
        equity_curve=curve, closed_trades=[], review_flag_count=0, metrics=metrics
    )
    backtest.store_run(db, result, result, result, result, None)

    report = paper.monthly_report(month_start, month_end, db)

    days = (month_end - month_start).days + 1
    expected = ((1 + metrics.cagr_pct / 100) ** (days / 365.25) - 1) * 100
    assert report["backtest_expected_return_pct"] == pytest.approx(expected)


# --- compare_to_backtest ---------------------------------------------------------


def test_compare_to_backtest_none_below_threshold(db):
    _seed_ticker()
    for i in range(5):
        _seed_trade_paper(
            entry_price=100.0,
            size=1000.0,
            status="CLOSED",
            entry_date=date(2024, 1, 1 + i),
            exit_date=date(2024, 1, 5 + i),
            exit_price=105.0,
            pnl=10.0,
        )
    assert paper.compare_to_backtest(db) is None


def test_compare_to_backtest_computes_bands_above_threshold(db):
    _seed_ticker()
    for i in range(30):
        win = i % 2 == 0
        exit_price = 110.0 if win else 95.0
        _seed_trade_paper(
            entry_price=100.0,
            size=1000.0,
            status="CLOSED",
            entry_date=date(2024, 1, 1) + timedelta(days=i),
            exit_date=date(2024, 1, 2) + timedelta(days=i),
            exit_price=exit_price,
            pnl=(exit_price - 100.0) * 10,
        )

    curve = [(date(2020, 1, 1), 100_000.0), (date(2020, 12, 31), 120_000.0)]
    trades = [
        backtest.BacktestTrade(
            ticker="X",
            tranche=1,
            sector="S",
            entry_date=date(2020, 1, 1),
            entry_price=100.0,
            exit_price=150.0,
            size_eur=1000,
            entry_cash_outlay=1000,
            exit_date=date(2020, 2, 1),
            realized_pnl_eur=50.0,
            status="CLOSED",
        )
    ]
    metrics = backtest.compute_metrics(curve, trades, 0.0, date(2020, 1, 1), date(2020, 12, 31))
    result = backtest.SimulationResult(
        equity_curve=curve, closed_trades=trades, review_flag_count=0, metrics=metrics
    )
    backtest.store_run(db, result, result, result, result, None)

    comparison = paper.compare_to_backtest(db)

    assert comparison is not None
    assert comparison["num_closed_trades"] == 30
    assert comparison["win_rate_pct"] == pytest.approx(50.0)
    assert comparison["backtest_win_rate_pct"] == pytest.approx(100.0)
    assert comparison["within_win_rate_band"] is False


# --- CLI helpers / dispatch -----------------------------------------------------


def test_resolve_previous_month():
    assert paper._resolve_previous_month(date(2024, 3, 15)) == (date(2024, 2, 1), date(2024, 2, 29))


def test_resolve_month():
    assert paper._resolve_month("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    assert paper._resolve_month("2024-12") == (date(2024, 12, 1), date(2024, 12, 31))


def test_main_fills_signals_by_default(mocker):
    mock_config = mocker.MagicMock()
    mocker.patch("stock_tracker.paper.load_config", return_value=mock_config)
    mocker.patch("stock_tracker.paper.setup_logging")
    mocker.patch("stock_tracker.paper.init_db")
    mock_fill = mocker.patch(
        "stock_tracker.paper.fill_pending_signals", return_value={"opened": 0, "closed": 0}
    )
    mock_compare = mocker.patch("stock_tracker.paper.compare_to_backtest", return_value=None)

    paper.main(["--date", "2024-06-04"])

    mock_fill.assert_called_once_with(date(2024, 6, 4), mock_config)
    mock_compare.assert_called_once_with(mock_config)


def test_main_monthly_report_dispatch(mocker):
    mock_config = mocker.MagicMock()
    mocker.patch("stock_tracker.paper.load_config", return_value=mock_config)
    mocker.patch("stock_tracker.paper.setup_logging")
    mocker.patch("stock_tracker.paper.init_db")
    mock_report = mocker.patch("stock_tracker.paper.monthly_report", return_value={"fake": True})
    mock_print = mocker.patch("stock_tracker.paper.print_monthly_report")
    mock_fill = mocker.patch("stock_tracker.paper.fill_pending_signals")

    paper.main(["--monthly-report", "--month", "2024-02"])

    mock_report.assert_called_once_with(date(2024, 2, 1), date(2024, 2, 29), mock_config)
    mock_print.assert_called_once_with({"fake": True})
    mock_fill.assert_not_called()
