from datetime import date, timedelta

import pytest

from stock_tracker import backtest
from stock_tracker.db.models import PriceDaily, QualityScore, Ticker
from stock_tracker.db.session import get_session

AS_OF = date(2024, 6, 3)  # Monday


# --- helpers -----------------------------------------------------------------


def _make_portfolio(cash=100_000.0):
    sizing = backtest.SizingConfig(
        max_pct_per_tranche=2.0, max_pct_per_ticker=6.0, max_pct_per_sector=25.0, min_cash_pct=20.0
    )
    costs = backtest.CostConfig(slippage_pct=0.1, tob_pct=0.35, broker_fee_pct=0.0, tax_pct=10.0)
    return backtest.Portfolio(cash=cash, sizing=sizing, costs=costs)


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


def _seed_eligible(ticker, as_of, eligible):
    with get_session() as session:
        session.add(
            QualityScore(
                ticker=ticker,
                date=as_of,
                score=6 if eligible else 0,
                eligible=eligible,
                market_cap_pass=eligible,
                fcf_positive_pass=eligible,
                revenue_growth_pass=eligible,
                net_debt_ebitda_pass=eligible,
                eps_estimate_trend_pass=eligible,
                volume_pass=eligible,
            )
        )


def _seed_price_path(ticker, dates_prices):
    with get_session() as session:
        for d, price in dates_prices:
            session.add(
                PriceDaily(
                    ticker=ticker,
                    date=d,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=1_000_000,
                    close_eur=price,
                )
            )


def _seed_flat_then_drop(ticker, as_of, num_days, baseline, today_close):
    with get_session() as session:
        for i in range(num_days):
            d = as_of - timedelta(days=num_days - 1 - i)
            close = today_close if d == as_of else baseline
            session.add(
                PriceDaily(
                    ticker=ticker,
                    date=d,
                    open=close,
                    high=close,
                    low=close,
                    close=close * 1.1,  # deliberately different from close_eur
                    volume=1_000_000,
                    close_eur=close,
                )
            )


def _metrics(cagr, sharpe):
    return backtest.Metrics(
        start=date(2020, 1, 1),
        end=date(2020, 12, 31),
        cagr_pct=cagr,
        max_drawdown_pct=-10.0,
        longest_underwater_days=30,
        win_rate_pct=50.0,
        avg_holding_period_days=20.0,
        num_trades=10,
        sharpe=sharpe,
        final_equity_eur=110_000.0,
    )


# --- Portfolio -----------------------------------------------------------------


def test_enter_applies_slippage_and_deducts_cash_and_costs():
    p = _make_portfolio()
    trade = p.enter("AAPL", 1, "Technology", date(2024, 1, 2), 100.0, 2000.0)

    assert trade.entry_price == pytest.approx(100.0 * 1.001)
    cost = 2000.0 * 0.0035
    assert p.cash == pytest.approx(100_000.0 - 2000.0 - cost)
    assert trade in p.open_trades


def test_exit_realizes_pnl_and_updates_cash():
    p = _make_portfolio()
    trade = p.enter("AAPL", 1, "Technology", date(2024, 1, 2), 100.0, 2000.0)
    cash_after_entry = p.cash

    p.exit_trade(trade, date(2024, 2, 1), 120.0, "profit_target")

    assert trade.status == "CLOSED"
    assert trade not in p.open_trades
    assert trade in p.closed_trades
    fill = 120.0 * (1 - 0.001)
    gross = 2000.0 * (fill / trade.entry_price)
    net = gross - gross * 0.0035
    assert trade.realized_pnl_eur == pytest.approx(net - trade.entry_cash_outlay)
    assert p.cash == pytest.approx(cash_after_entry + net)
    assert p.realized_pnl_by_year[2024] == pytest.approx(trade.realized_pnl_eur)


def test_can_enter_blocked_by_cash_floor():
    allowed = _make_portfolio(cash=25_000.0).can_enter(
        "AAPL", "Technology", 2000.0, equity=100_000.0, price_lookup=lambda t: None
    )
    assert allowed is True

    blocked = _make_portfolio(cash=21_000.0).can_enter(
        "AAPL", "Technology", 2000.0, equity=100_000.0, price_lookup=lambda t: None
    )
    assert blocked is False


def test_can_enter_blocked_by_ticker_exposure_cap():
    p = _make_portfolio()
    for tranche in (1, 2, 3):
        p.enter("AAPL", tranche, "Technology", date(2024, 1, 1), 100.0, 2000.0)

    blocked = p.can_enter(
        "AAPL", "Technology", 2000.0, equity=100_000.0, price_lookup=lambda t: 100.0
    )
    assert blocked is False


def test_can_enter_blocked_by_sector_exposure_cap():
    p = _make_portfolio(cash=1_000_000.0)
    for ticker in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M"]:
        p.enter(ticker, 1, "Technology", date(2024, 1, 1), 100.0, 2000.0)

    blocked = p.can_enter(
        "NEW", "Technology", 2000.0, equity=100_000.0, price_lookup=lambda t: 100.0
    )
    assert blocked is False


def test_settle_tax_deducts_ten_percent_of_positive_net_gain():
    p = _make_portfolio()
    p.realized_pnl_by_year[2024] = 1000.0
    cash_before = p.cash

    p.settle_tax(2024)

    assert p.cash == pytest.approx(cash_before - 100.0)
    assert p.tax_paid_by_year[2024] == pytest.approx(100.0)


def test_settle_tax_no_tax_on_net_loss_year():
    p = _make_portfolio()
    p.realized_pnl_by_year[2024] = -500.0
    cash_before = p.cash

    p.settle_tax(2024)

    assert p.cash == cash_before
    assert 2024 not in p.tax_paid_by_year


# --- metrics -------------------------------------------------------------------


def test_compute_metrics_flat_curve():
    curve = [(date(2020, 1, 1) + timedelta(days=i), 100_000.0) for i in range(10)]
    m = backtest.compute_metrics(curve, [], 0.0, date(2020, 1, 1), date(2020, 1, 10))
    assert m.cagr_pct == pytest.approx(0.0, abs=1e-6)
    assert m.max_drawdown_pct == pytest.approx(0.0)
    assert m.sharpe == 0.0


def test_compute_metrics_cagr_growth():
    start, end = date(2020, 1, 1), date(2021, 1, 1)
    curve = [(start, 100_000.0), (end, 200_000.0)]
    m = backtest.compute_metrics(curve, [], 0.0, start, end)
    years = (end - start).days / 365.25
    expected = ((200_000.0 / 100_000.0) ** (1 / years) - 1) * 100
    assert m.cagr_pct == pytest.approx(expected)
    assert m.final_equity_eur == pytest.approx(200_000.0)


def test_compute_metrics_drawdown_and_underwater_days():
    d0 = date(2020, 1, 1)
    curve = [
        (d0, 100.0),
        (d0 + timedelta(days=1), 120.0),
        (d0 + timedelta(days=2), 90.0),
        (d0 + timedelta(days=3), 100.0),
        (d0 + timedelta(days=4), 130.0),
    ]
    m = backtest.compute_metrics(curve, [], 0.0, d0, d0 + timedelta(days=4))
    assert m.max_drawdown_pct == pytest.approx((90.0 / 120.0 - 1) * 100)
    assert m.longest_underwater_days == 2


def test_compute_metrics_win_rate_and_avg_holding_period():
    trades = [
        backtest.BacktestTrade(
            ticker="A",
            tranche=1,
            sector="S",
            entry_date=date(2024, 1, 1),
            entry_price=100,
            size_eur=1000,
            entry_cash_outlay=1000,
            exit_date=date(2024, 1, 11),
            realized_pnl_eur=50.0,
            status="CLOSED",
        ),
        backtest.BacktestTrade(
            ticker="B",
            tranche=1,
            sector="S",
            entry_date=date(2024, 1, 1),
            entry_price=100,
            size_eur=1000,
            entry_cash_outlay=1000,
            exit_date=date(2024, 1, 21),
            realized_pnl_eur=-20.0,
            status="CLOSED",
        ),
    ]
    curve = [(date(2024, 1, 1), 100_000.0), (date(2024, 1, 21), 101_000.0)]
    m = backtest.compute_metrics(curve, trades, 0.0, date(2024, 1, 1), date(2024, 1, 21))
    assert m.num_trades == 2
    assert m.win_rate_pct == pytest.approx(50.0)
    assert m.avg_holding_period_days == pytest.approx((10 + 20) / 2)


def test_compute_metrics_empty_curve():
    m = backtest.compute_metrics([], [], 0.0, date(2020, 1, 1), date(2020, 12, 31))
    assert m.num_trades == 0
    assert m.win_rate_pct is None
    assert m.final_equity_eur == 0.0


# --- EUR indicators / tranche selection ----------------------------------------


def test_indicators_eur_reads_close_eur_not_native_close(db):
    _seed_ticker()
    _seed_flat_then_drop("AAPL", AS_OF, 60, baseline=100, today_close=70)

    with get_session() as session:
        ind = backtest.indicators_eur(session, "AAPL", AS_OF, db)

    assert ind.close == 70
    assert ind.sma50 == pytest.approx((49 * 100 + 70) / 50)


def test_entry_tranche_eur_boundaries(db):
    ind3 = backtest.EurIndicators(date=AS_OF, close=70.0, sma50=99.4, rsi14=50.0)
    assert backtest.entry_tranche_eur(ind3, db) == 3

    ind_none = backtest.EurIndicators(date=AS_OF, close=99.0, sma50=99.4, rsi14=50.0)
    assert backtest.entry_tranche_eur(ind_none, db) is None


# --- overfitting check -----------------------------------------------------------


def test_check_overfitting_flags_cagr_degradation(db):
    warning = backtest.check_overfitting(_metrics(20.0, 1.0), _metrics(5.0, 0.8), db)
    assert warning is not None
    assert "CAGR" in warning


def test_check_overfitting_flags_sharpe_breakdown(db):
    warning = backtest.check_overfitting(_metrics(10.0, 1.0), _metrics(9.0, -0.5), db)
    assert warning is not None
    assert "Sharpe" in warning


def test_check_overfitting_no_warning_when_consistent(db):
    assert backtest.check_overfitting(_metrics(10.0, 1.0), _metrics(9.0, 0.9), db) is None


# --- config hash -----------------------------------------------------------------


def test_config_hash_deterministic(db):
    assert backtest.config_hash(db) == backtest.config_hash(db)


def test_config_hash_changes_with_content():
    from stock_tracker.config import Config

    assert backtest.config_hash(Config({"a": 1})) != backtest.config_hash(Config({"a": 2}))


# --- end-to-end simulate() --------------------------------------------------------


def test_simulate_enters_on_dip_and_exits_on_profit_target(db):
    start = date(2024, 3, 4)
    end = date(2024, 3, 29)
    history_start = start - timedelta(days=60)
    drop_date = date(2024, 3, 6)
    jump_date = date(2024, 3, 8)

    _seed_ticker("AAPL", currency="EUR", sector="Technology")
    _seed_eligible("AAPL", date(2024, 3, 1), True)

    prices = []
    d = history_start
    while d <= end:
        if drop_date <= d < jump_date:
            price = 70.0
        elif d >= jump_date:
            price = 115.0
        else:
            price = 100.0
        prices.append((d, price))
        d += timedelta(days=1)
    _seed_price_path("AAPL", prices)

    result = backtest.simulate(start, end, db)

    assert len(result.closed_trades) == 1
    trade = result.closed_trades[0]
    assert trade.entry_date == drop_date
    assert trade.tranche == 3
    assert trade.exit_date == jump_date
    assert trade.exit_reason == "profit_target"
    assert trade.realized_pnl_eur > 0
    assert result.equity_curve[-1][1] > db.backtest.initial_capital_eur


def test_simulate_settles_capital_gains_tax_at_year_boundary(db):
    start = date(2024, 3, 4)
    end = date(2025, 1, 3)
    history_start = start - timedelta(days=60)
    drop_date = date(2024, 3, 6)
    jump_date = date(2024, 3, 8)

    _seed_ticker("AAPL", currency="EUR", sector="Technology")
    _seed_eligible("AAPL", date(2024, 3, 1), True)

    prices = []
    d = history_start
    while d <= end:
        if drop_date <= d < jump_date:
            price = 70.0
        elif d >= jump_date:
            price = 115.0
        else:
            price = 100.0
        prices.append((d, price))
        d += timedelta(days=1)
    _seed_price_path("AAPL", prices)

    result = backtest.simulate(start, end, db)

    assert len(result.closed_trades) == 1
    trade = result.closed_trades[0]
    assert trade.realized_pnl_eur > 0
    expected_tax = trade.realized_pnl_eur * db.backtest.costs.capital_gains_tax_pct / 100

    curve = dict(result.equity_curve)
    dec31 = curve[date(2024, 12, 31)]
    jan1 = curve[date(2025, 1, 1)]
    assert dec31 - jan1 == pytest.approx(expected_tax, abs=1e-6)


# --- persistence -------------------------------------------------------------------


def test_store_and_list_runs(db):
    def _dummy_result(cagr):
        curve = [(date(2020, 1, 1), 100_000.0), (date(2020, 12, 31), 100_000 * (1 + cagr / 100))]
        metrics = backtest.compute_metrics(curve, [], 0.0, date(2020, 1, 1), date(2020, 12, 31))
        return backtest.SimulationResult(
            equity_curve=curve, closed_trades=[], review_flag_count=0, metrics=metrics
        )

    run_id = backtest.store_run(
        db, _dummy_result(20), _dummy_result(5), _dummy_result(10), _dummy_result(8), "test warning"
    )

    runs = backtest.list_runs()
    assert len(runs) == 1
    assert runs[0]["id"] == run_id
    assert runs[0]["notes"] == "test warning"
    assert set(runs[0]["metrics"].keys()) == {
        "in_sample_strategy",
        "in_sample_benchmark",
        "out_of_sample_strategy",
        "out_of_sample_benchmark",
    }


# --- run() orchestration ------------------------------------------------------------


def test_resolve_period_end_uses_configured_value(db):
    with get_session() as session:
        assert backtest._resolve_period_end(session, "2019-06-15") == date(2019, 6, 15)


def test_resolve_period_end_raises_without_price_data(db):
    with get_session() as session, pytest.raises(RuntimeError):
        backtest._resolve_period_end(session, None)


def test_run_resolves_dates_from_config_and_stores(db, mocker):
    with get_session() as session:
        session.add(
            Ticker(ticker="AAPL", name="x", exchange="US", currency="EUR", index_membership="SP500")
        )
        session.add(
            PriceDaily(
                ticker="AAPL",
                date=date(2024, 6, 1),
                open=1,
                high=1,
                low=1,
                close=1,
                volume=1,
                close_eur=1,
            )
        )

    fake_sim = backtest.SimulationResult(
        equity_curve=[],
        closed_trades=[],
        review_flag_count=0,
        metrics=backtest.compute_metrics([], [], 0.0, date(2012, 1, 1), date(2019, 12, 31)),
    )
    mock_simulate = mocker.patch("stock_tracker.backtest.simulate", return_value=fake_sim)
    mocker.patch("stock_tracker.backtest.simulate_benchmark", return_value=fake_sim)
    mock_store = mocker.patch("stock_tracker.backtest.store_run", return_value=42)

    result = backtest.run(db)

    assert result["run_id"] == 42
    calls = mock_simulate.call_args_list
    assert calls[0].args[0] == date(2012, 1, 1)
    assert calls[0].args[1] == date(2019, 12, 31)
    assert calls[1].args[0] == date(2020, 1, 1)
    assert calls[1].args[1] == date(2024, 6, 1)
    mock_store.assert_called_once()


# --- CLI --------------------------------------------------------------------------


def test_main_list_runs_dispatch(mocker):
    mock_config = mocker.MagicMock()
    mocker.patch("stock_tracker.backtest.load_config", return_value=mock_config)
    mocker.patch("stock_tracker.backtest.setup_logging")
    mocker.patch("stock_tracker.backtest.init_db")
    mock_list = mocker.patch("stock_tracker.backtest.list_runs", return_value=[])
    mock_run = mocker.patch("stock_tracker.backtest.run")

    backtest.main(["--list-runs", "--limit", "5"])

    mock_list.assert_called_once_with(5)
    mock_run.assert_not_called()


def test_main_runs_backtest_and_prints_report(mocker):
    mock_config = mocker.MagicMock()
    mocker.patch("stock_tracker.backtest.load_config", return_value=mock_config)
    mocker.patch("stock_tracker.backtest.setup_logging")
    mocker.patch("stock_tracker.backtest.init_db")
    mock_run = mocker.patch("stock_tracker.backtest.run", return_value={"fake": "result"})
    mock_print = mocker.patch("stock_tracker.backtest.print_report")

    backtest.main(["--in-start", "2020-01-01"])

    mock_run.assert_called_once_with(
        mock_config, in_start=date(2020, 1, 1), in_end=None, out_start=None, out_end=None
    )
    mock_print.assert_called_once_with({"fake": "result"})
