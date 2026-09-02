from datetime import date, timedelta

import pytest

from stock_tracker import signals
from stock_tracker.db.models import (
    AnalystEstimate,
    EarningsCalendar,
    PriceDaily,
    QualityScore,
    Ticker,
    TradePaper,
)
from stock_tracker.db.session import get_session

AS_OF = date(2024, 6, 3)  # Monday


def _seed_ticker(ticker="AAPL", currency="EUR"):
    with get_session() as session:
        session.add(
            Ticker(
                ticker=ticker,
                name="Test Co",
                exchange="US",
                currency=currency,
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


def _seed_prices(ticker, as_of, num_days, baseline, today_close, volume=1_000_000):
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
                    close=close,
                    volume=volume,
                    close_eur=close,
                )
            )


def _seed_flat(ticker, as_of, num_days, price, volume=1_000_000):
    _seed_prices(ticker, as_of, num_days, price, price, volume)


def _seed_earnings(ticker, earnings_date, confirmed=True):
    with get_session() as session:
        session.add(
            EarningsCalendar(
                ticker=ticker, earnings_date=earnings_date, confirmed=confirmed, source="fmp"
            )
        )


def _seed_estimate(ticker, as_of_date, fiscal_year, eps_estimate):
    with get_session() as session:
        session.add(
            AnalystEstimate(
                ticker=ticker,
                as_of_date=as_of_date,
                fiscal_year=fiscal_year,
                eps_estimate=eps_estimate,
                source="fmp",
            )
        )


def _seed_trade(ticker, tranche, entry_date, entry_price, status="OPEN"):
    with get_session() as session:
        session.add(
            TradePaper(
                ticker=ticker,
                tranche=tranche,
                entry_date=entry_date,
                entry_price=entry_price,
                size=1.0,
                status=status,
            )
        )


def _indicators(ticker="AAPL", as_of=AS_OF, config=None):
    from stock_tracker.config import load_config

    config = config or load_config()
    with get_session() as session:
        return signals.compute_indicators(session, ticker, as_of, config)


# --- indicators -----------------------------------------------------------


def test_compute_indicators_values(db):
    _seed_ticker()
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=70)

    result = _indicators(config=db)

    assert result is not None
    assert result.close == 70
    assert result.sma50 == pytest.approx((49 * 100 + 70) / 50)  # last 50 of the 60 seeded closes
    assert result.rsi14 == pytest.approx(0.0)
    assert result.high_52w == 100
    assert result.distance_to_52w_high_pct == pytest.approx(-30.0)


def test_compute_indicators_none_without_bar_for_as_of(db):
    _seed_ticker()
    _seed_prices("AAPL", AS_OF - timedelta(days=1), 60, baseline=100, today_close=100)

    assert _indicators(config=db) is None


# --- entry tranche selection ----------------------------------------------


def test_tranche_3_deep_discount(db):
    _seed_ticker()
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=70)
    result = _indicators(config=db)
    assert signals._entry_tranche(result, db) == 3


def test_tranche_2_moderate_discount(db):
    _seed_ticker()
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=84)
    result = _indicators(config=db)
    assert signals._entry_tranche(result, db) == 2


def test_tranche_1_requires_price_and_rsi(db):
    _seed_ticker()
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=91)
    result = _indicators(config=db)
    assert result.rsi14 < 35
    assert signals._entry_tranche(result, db) == 1


def test_no_entry_when_discount_insufficient(db):
    _seed_ticker()
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=97)
    result = _indicators(config=db)
    assert signals._entry_tranche(result, db) is None


# --- earnings guard ---------------------------------------------------------


def test_earnings_guard_suppresses_within_window(db):
    _seed_ticker()
    _seed_earnings("AAPL", date(2024, 6, 6))  # 3 trading days after AS_OF
    with get_session() as session:
        assert signals._within_earnings_guard(session, "AAPL", AS_OF, 5) is True


def test_earnings_guard_allows_outside_window(db):
    _seed_ticker()
    _seed_earnings("AAPL", date(2024, 6, 11))  # 6 trading days after AS_OF
    with get_session() as session:
        assert signals._within_earnings_guard(session, "AAPL", AS_OF, 5) is False


def test_earnings_guard_allows_when_no_upcoming_earnings(db):
    _seed_ticker()
    with get_session() as session:
        assert signals._within_earnings_guard(session, "AAPL", AS_OF, 5) is False


# --- generate_entry_signals -------------------------------------------------


def test_generate_entry_signals_only_for_eligible(db):
    _seed_ticker("AAPL")
    _seed_eligible("AAPL", AS_OF, True)
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=70)

    _seed_ticker("MSFT")
    _seed_eligible("MSFT", AS_OF, False)
    _seed_prices("MSFT", AS_OF, 60, baseline=100, today_close=70)

    results = signals.generate_entry_signals(AS_OF, db)

    tickers = {r.ticker for r in results}
    assert tickers == {"AAPL"}
    assert results[0].tranche == 3
    assert results[0].signal_type == signals.ENTRY


def test_generate_entry_signals_suppressed_by_earnings_guard(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF, True)
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=70)
    _seed_earnings("AAPL", date(2024, 6, 6))

    results = signals.generate_entry_signals(AS_OF, db)

    assert results == []


# --- generate_exit_signals --------------------------------------------------


def test_exit_on_profit_target(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF, True)
    _seed_flat("AAPL", AS_OF, 60, price=100)
    _seed_trade("AAPL", tranche=1, entry_date=AS_OF - timedelta(weeks=2), entry_price=90)

    results = signals.generate_exit_signals(AS_OF, db)

    exits = [r for r in results if r.signal_type == signals.EXIT]
    assert len(exits) == 1
    assert exits[0].notes == "profit_target"
    assert exits[0].tranche == 1


def test_exit_on_close_above_sma50(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF, True)
    _seed_prices("AAPL", AS_OF, 60, baseline=90, today_close=100)
    _seed_trade("AAPL", tranche=1, entry_date=AS_OF - timedelta(weeks=2), entry_price=95)

    results = signals.generate_exit_signals(AS_OF, db)

    exits = [r for r in results if r.signal_type == signals.EXIT]
    assert len(exits) == 1
    assert exits[0].notes == "close_above_sma50"


def test_review_flag_on_time_stop_without_recovery(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF, True)
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=84)
    _seed_trade("AAPL", tranche=2, entry_date=AS_OF - timedelta(weeks=13), entry_price=100)

    results = signals.generate_exit_signals(AS_OF, db)

    review = [r for r in results if r.signal_type == signals.REVIEW]
    assert len(review) == 1
    assert review[0].tranche == 2
    assert "weeks" in review[0].notes


def test_no_review_when_recovered_even_if_old(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF, True)
    _seed_flat("AAPL", AS_OF, 60, price=100)
    _seed_trade("AAPL", tranche=1, entry_date=AS_OF - timedelta(weeks=13), entry_price=90)

    results = signals.generate_exit_signals(AS_OF, db)

    assert [r.signal_type for r in results if r.tranche == 1] == [signals.EXIT]


def test_fundamental_exit_when_not_eligible(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF, False)
    _seed_flat("AAPL", AS_OF, 60, price=100)
    _seed_trade("AAPL", tranche=1, entry_date=AS_OF - timedelta(weeks=1), entry_price=100)

    results = signals.generate_exit_signals(AS_OF, db)

    fundamental = [r for r in results if r.signal_type == signals.EXIT_FUNDAMENTAL]
    assert len(fundamental) == 1
    assert fundamental[0].tranche is None
    assert "no longer eligible" in fundamental[0].notes


def test_fundamental_exit_when_eps_estimate_dropped(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF, True)
    _seed_flat("AAPL", AS_OF, 60, price=100)
    _seed_trade("AAPL", tranche=1, entry_date=AS_OF - timedelta(weeks=1), entry_price=100)
    _seed_estimate("AAPL", AS_OF - timedelta(days=90), 2025, 10.0)
    _seed_estimate("AAPL", AS_OF, 2025, 8.0)

    results = signals.generate_exit_signals(AS_OF, db)

    fundamental = [r for r in results if r.signal_type == signals.EXIT_FUNDAMENTAL]
    assert len(fundamental) == 1
    assert "eps estimate dropped" in fundamental[0].notes
    assert "no longer eligible" not in fundamental[0].notes


def test_no_fundamental_exit_when_stable(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF, True)
    _seed_flat("AAPL", AS_OF, 60, price=100)
    _seed_trade("AAPL", tranche=1, entry_date=AS_OF - timedelta(weeks=1), entry_price=100)

    results = signals.generate_exit_signals(AS_OF, db)

    assert [r for r in results if r.signal_type == signals.EXIT_FUNDAMENTAL] == []


# --- storage / orchestration ------------------------------------------------


def test_store_signals_upserts(db):
    result = signals.SignalResult(
        ticker="AAPL",
        date=AS_OF,
        signal_type=signals.ENTRY,
        tranche=1,
        price=91.0,
        sma50=99.0,
        rsi14=10.0,
        distance_52w_high=-9.0,
    )
    signals.store_signals([result])
    updated = signals.SignalResult(**{**result.__dict__, "price": 92.0})
    signals.store_signals([updated])

    with get_session() as session:
        from stock_tracker.db.models import Signal

        assert session.query(Signal).count() == 1
        assert session.query(Signal).one().price == 92.0


def test_run_for_date_end_to_end(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF, True)
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=70)

    stored = signals.run_for_date(AS_OF, db)

    assert stored == 1
    with get_session() as session:
        from stock_tracker.db.models import Signal

        row = session.query(Signal).filter_by(ticker="AAPL", date=AS_OF).one()
        assert row.signal_type == signals.ENTRY
        assert row.tranche == 3


def test_main_requires_valid_date_format():
    with pytest.raises(SystemExit):
        signals.main(["--date", "not-a-date"])


def test_main_dispatches_to_run_for_date(mocker):
    mock_config = mocker.MagicMock()
    mocker.patch("stock_tracker.signals.load_config", return_value=mock_config)
    mocker.patch("stock_tracker.signals.setup_logging")
    mocker.patch("stock_tracker.signals.init_db")
    mock_run = mocker.patch("stock_tracker.signals.run_for_date")

    signals.main(["--date", "2024-06-03"])

    mock_run.assert_called_once_with(date(2024, 6, 3), mock_config)
