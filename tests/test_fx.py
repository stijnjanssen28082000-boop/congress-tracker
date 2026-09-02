from datetime import date

import pandas as pd
import pytest

from stock_tracker.fx import convert_to_eur, fetch_fx_rates, get_rate_to_eur, store_fx_rates


def test_eur_rate_is_always_one(db):
    assert get_rate_to_eur("EUR", date(2024, 1, 1)) == 1.0


def test_store_and_lookup_rate(db):
    store_fx_rates("USD", {date(2024, 1, 2): 0.90, date(2024, 1, 3): 0.91})
    assert get_rate_to_eur("USD", date(2024, 1, 3)) == pytest.approx(0.91)
    # Weekend/holiday: falls back to the most recent known rate.
    assert get_rate_to_eur("USD", date(2024, 1, 5)) == pytest.approx(0.91)


def test_store_fx_rates_updates_existing(db):
    store_fx_rates("USD", {date(2024, 1, 2): 0.90})
    store_fx_rates("USD", {date(2024, 1, 2): 0.95})
    assert get_rate_to_eur("USD", date(2024, 1, 2)) == pytest.approx(0.95)


def test_missing_rate_raises_lookup_error(db):
    with pytest.raises(LookupError):
        get_rate_to_eur("USD", date(2024, 1, 1))


def test_convert_to_eur(db):
    store_fx_rates("USD", {date(2024, 1, 2): 0.90})
    assert convert_to_eur(100, "USD", date(2024, 1, 2)) == pytest.approx(90.0)


def test_convert_to_eur_noop_for_eur(db):
    assert convert_to_eur(100, "EUR", date(2024, 1, 2)) == 100.0


def test_fetch_fx_rates_eur_returns_empty():
    assert fetch_fx_rates("EUR", date(2024, 1, 1), date(2024, 1, 5)) == {}


def test_fetch_fx_rates_uses_yfinance(mocker):
    fake_history = pd.DataFrame(
        {"Close": [0.90, 0.91]},
        index=pd.to_datetime([date(2024, 1, 2), date(2024, 1, 3)]),
    )
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = fake_history
    mocker.patch("stock_tracker.fx.yf.Ticker", return_value=mock_ticker)

    rates = fetch_fx_rates("USD", date(2024, 1, 2), date(2024, 1, 3))

    assert rates[date(2024, 1, 2)] == 0.90
    assert rates[date(2024, 1, 3)] == 0.91


def test_fetch_fx_rates_empty_history(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()
    mocker.patch("stock_tracker.fx.yf.Ticker", return_value=mock_ticker)

    assert fetch_fx_rates("USD", date(2024, 1, 2), date(2024, 1, 3)) == {}
