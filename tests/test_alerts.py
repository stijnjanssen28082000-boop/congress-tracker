from datetime import date

from stock_tracker import alerts
from stock_tracker.signals import ENTRY, EXIT, EXIT_FUNDAMENTAL, REVIEW, SignalResult

AS_OF = date(2024, 6, 3)


def _entry(ticker="AAPL", tranche=3, price=70.0, rsi=20.0):
    return SignalResult(
        ticker=ticker,
        date=AS_OF,
        signal_type=ENTRY,
        tranche=tranche,
        price=price,
        sma50=99.0,
        rsi14=rsi,
        distance_52w_high=-30.0,
    )


def _exit(ticker="AAPL", tranche=1, signal_type=EXIT, notes="profit_target"):
    return SignalResult(
        ticker=ticker,
        date=AS_OF,
        signal_type=signal_type,
        tranche=tranche,
        price=110.0,
        sma50=99.0,
        rsi14=60.0,
        distance_52w_high=-2.0,
        notes=notes,
    )


def _review(ticker="AAPL", tranche=2):
    return SignalResult(
        ticker=ticker,
        date=AS_OF,
        signal_type=REVIEW,
        tranche=tranche,
        price=90.0,
        sma50=99.0,
        rsi14=40.0,
        distance_52w_high=-10.0,
        notes="open 13.0 weeks without recovery",
    )


def test_format_daily_alert_returns_none_when_nothing():
    assert alerts.format_daily_alert(AS_OF, [], []) is None


def test_format_daily_alert_includes_entries():
    text = alerts.format_daily_alert(AS_OF, [_entry()], [])
    assert text is not None
    assert "AAPL" in text
    assert "T3" in text
    assert "Nieuwe entries" in text


def test_format_daily_alert_includes_exits_and_reviews_separately():
    text = alerts.format_daily_alert(AS_OF, [], [_exit(), _review()])
    assert "Exits" in text
    assert "profit_target" in text
    assert "REVIEW-flags" in text
    assert "without recovery" in text


def test_format_daily_alert_fundamental_exit_shows_all_tranches_label():
    sig = _exit(tranche=None, signal_type=EXIT_FUNDAMENTAL, notes="no longer eligible")
    text = alerts.format_daily_alert(AS_OF, [], [sig])
    assert "alle tranches" in text
    assert "no longer eligible" in text


def test_format_milestone_alert():
    milestone = {
        "num_closed_trades": 30,
        "win_rate_pct": 55.0,
        "backtest_win_rate_pct": 60.0,
        "avg_profit_pct": 3.2,
        "backtest_avg_profit_pct": 4.0,
    }
    text = alerts.format_milestone_alert(milestone)
    assert "30" in text
    assert "55.0%" in text
    assert "3.20%" in text


def test_send_telegram_message_without_config_returns_false(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert alerts.send_telegram_message("hello") is False


def test_send_telegram_message_posts_when_configured(monkeypatch, mocker):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    mock_response = mocker.MagicMock()
    mock_post = mocker.patch("stock_tracker.alerts.requests.post", return_value=mock_response)

    result = alerts.send_telegram_message("hello")

    assert result is True
    mock_post.assert_called_once()
    url = mock_post.call_args.args[0]
    assert "tok" in url
    assert mock_post.call_args.kwargs["json"] == {"chat_id": "123", "text": "hello"}


def test_send_telegram_message_handles_request_exception(monkeypatch, mocker):
    import requests

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    mocker.patch(
        "stock_tracker.alerts.requests.post", side_effect=requests.RequestException("boom")
    )

    assert alerts.send_telegram_message("hello") is False
