from datetime import date

import pytest

from stock_tracker import run_daily
from stock_tracker.db.models import PriceDaily, Ticker, TradePaper
from stock_tracker.db.session import get_session
from stock_tracker.signals import SignalResult

MONDAY = date(2024, 6, 3)
TUESDAY = date(2024, 6, 4)


def _entry_signal(ticker="AAPL"):
    return SignalResult(
        ticker=ticker,
        date=MONDAY,
        signal_type="ENTRY",
        tranche=1,
        price=90.0,
        sma50=99.0,
        rsi14=20.0,
        distance_52w_high=-9.0,
    )


def _mock_pipeline(mocker, entries=None, exits=None, fill_result=None):
    mock_ingest = mocker.patch("stock_tracker.run_daily.ingest.run_daily")
    mock_quality = mocker.patch("stock_tracker.run_daily.quality.run_for_date")
    mocker.patch(
        "stock_tracker.run_daily.signals.generate_entry_signals", return_value=entries or []
    )
    mocker.patch(
        "stock_tracker.run_daily.signals.generate_exit_signals", return_value=exits or []
    )
    mocker.patch("stock_tracker.run_daily.signals.store_signals")
    mocker.patch(
        "stock_tracker.run_daily.paper.fill_pending_signals",
        return_value=fill_result or {"opened": 0, "closed": 0},
    )
    return mock_ingest, mock_quality


def test_is_quality_day_matches_configured_weekday(db):
    assert run_daily._is_quality_day(MONDAY, db) is True
    assert run_daily._is_quality_day(TUESDAY, db) is False


def test_run_calls_pipeline_and_skips_quality_on_non_monday(db, mocker):
    mock_ingest, mock_quality = _mock_pipeline(mocker)
    mock_send = mocker.patch("stock_tracker.run_daily.send_telegram_message")

    result = run_daily.run(TUESDAY, db)

    mock_ingest.assert_called_once_with(db)
    mock_quality.assert_not_called()
    mock_send.assert_not_called()
    assert result["alert_sent"] is False


def test_run_calls_quality_on_configured_weekday(db, mocker):
    _mock_ingest, mock_quality = _mock_pipeline(mocker)
    mocker.patch("stock_tracker.run_daily.send_telegram_message")

    run_daily.run(MONDAY, db)

    mock_quality.assert_called_once_with(MONDAY, db)


def test_run_sends_alert_when_signals_exist(db, mocker):
    _mock_pipeline(mocker, entries=[_entry_signal()])
    mock_send = mocker.patch("stock_tracker.run_daily.send_telegram_message", return_value=True)

    result = run_daily.run(MONDAY, db)

    mock_send.assert_called_once()
    assert "AAPL" in mock_send.call_args.args[0]
    assert result["alert_sent"] is True


def test_run_no_alert_when_nothing_to_report(db, mocker):
    _mock_pipeline(mocker)
    mock_send = mocker.patch("stock_tracker.run_daily.send_telegram_message")

    result = run_daily.run(MONDAY, db)

    mock_send.assert_not_called()
    assert result["alert_sent"] is False


def test_run_sends_milestone_alert_on_crossing_threshold(db, mocker):
    with get_session() as session:
        session.add(
            Ticker(
                ticker="AAPL", name="x", exchange="US", currency="EUR", index_membership="SP500"
            )
        )
        for _ in range(29):
            session.add(
                TradePaper(
                    ticker="AAPL",
                    tranche=1,
                    entry_date=date(2024, 1, 1),
                    entry_price=100.0,
                    exit_date=date(2024, 1, 2),
                    exit_price=105.0,
                    size=1000.0,
                    status="CLOSED",
                    pnl=50.0,
                )
            )

    mocker.patch("stock_tracker.run_daily.ingest.run_daily")
    mocker.patch("stock_tracker.run_daily.quality.run_for_date")
    mocker.patch("stock_tracker.run_daily.signals.generate_entry_signals", return_value=[])
    mocker.patch("stock_tracker.run_daily.signals.generate_exit_signals", return_value=[])
    mocker.patch("stock_tracker.run_daily.signals.store_signals")

    def _fill_and_close_one_more(as_of, config):
        with get_session() as session:
            session.add(
                TradePaper(
                    ticker="AAPL",
                    tranche=2,
                    entry_date=date(2024, 1, 1),
                    entry_price=100.0,
                    exit_date=as_of,
                    exit_price=105.0,
                    size=1000.0,
                    status="CLOSED",
                    pnl=50.0,
                )
            )
        return {"opened": 0, "closed": 1}

    mocker.patch(
        "stock_tracker.run_daily.paper.fill_pending_signals", side_effect=_fill_and_close_one_more
    )
    mocker.patch(
        "stock_tracker.run_daily.paper.compare_to_backtest",
        return_value={
            "num_closed_trades": 30,
            "win_rate_pct": 100.0,
            "backtest_win_rate_pct": None,
            "avg_profit_pct": 5.0,
            "backtest_avg_profit_pct": None,
        },
    )
    mock_send = mocker.patch("stock_tracker.run_daily.send_telegram_message", return_value=True)

    result = run_daily.run(TUESDAY, db)

    assert result["milestone_sent"] is True
    assert mock_send.call_count == 1
    assert "Milestone" in mock_send.call_args.args[0]


def test_run_defaults_as_of_to_latest_ingested_price_date(db, mocker):
    with get_session() as session:
        session.add(
            Ticker(
                ticker="AAPL", name="x", exchange="US", currency="EUR", index_membership="SP500"
            )
        )
        session.add(
            PriceDaily(
                ticker="AAPL",
                date=MONDAY,
                open=100,
                high=100,
                low=100,
                close=100,
                volume=1,
                close_eur=100,
            )
        )

    mock_ingest = mocker.patch("stock_tracker.run_daily.ingest.run_daily")
    mock_quality = mocker.patch("stock_tracker.run_daily.quality.run_for_date")
    mock_entries = mocker.patch(
        "stock_tracker.run_daily.signals.generate_entry_signals", return_value=[]
    )
    mocker.patch("stock_tracker.run_daily.signals.generate_exit_signals", return_value=[])
    mocker.patch("stock_tracker.run_daily.signals.store_signals")
    mocker.patch(
        "stock_tracker.run_daily.paper.fill_pending_signals",
        return_value={"opened": 0, "closed": 0},
    )
    mocker.patch("stock_tracker.run_daily.send_telegram_message")

    result = run_daily.run(None, db)

    # Not date.today() -- the latest price actually ingested, even though
    # ingest.run_daily is mocked here and doesn't add any newer rows itself.
    assert result["as_of"] == MONDAY
    mock_ingest.assert_called_once_with(db)
    mock_quality.assert_called_once_with(MONDAY, db)
    mock_entries.assert_called_once_with(MONDAY, db)


def test_run_falls_back_to_today_without_any_price_data(db, mocker):
    mocker.patch("stock_tracker.run_daily.ingest.run_daily")
    mocker.patch("stock_tracker.run_daily.quality.run_for_date")
    mocker.patch("stock_tracker.run_daily.signals.generate_entry_signals", return_value=[])
    mocker.patch("stock_tracker.run_daily.signals.generate_exit_signals", return_value=[])
    mocker.patch("stock_tracker.run_daily.signals.store_signals")
    mocker.patch(
        "stock_tracker.run_daily.paper.fill_pending_signals",
        return_value={"opened": 0, "closed": 0},
    )
    mocker.patch("stock_tracker.run_daily.send_telegram_message")

    result = run_daily.run(None, db)

    assert result["as_of"] == date.today()


def test_main_dispatches_to_run(mocker):
    mock_config = mocker.MagicMock()
    mocker.patch("stock_tracker.run_daily.load_dotenv")
    mocker.patch("stock_tracker.run_daily.load_config", return_value=mock_config)
    mocker.patch("stock_tracker.run_daily.setup_logging")
    mocker.patch("stock_tracker.run_daily.init_db")
    mock_run = mocker.patch("stock_tracker.run_daily.run", return_value={"fake": True})

    run_daily.main(["--date", "2024-06-03"])

    mock_run.assert_called_once_with(date(2024, 6, 3), mock_config)


def test_main_requires_valid_date_format():
    with pytest.raises(SystemExit):
        run_daily.main(["--date", "not-a-date"])
