from datetime import date

import pandas as pd
import pytest

from stock_tracker.providers.fmp_provider import FMPProvider
from stock_tracker.providers.yfinance_provider import YFinanceProvider


def test_yfinance_provider_get_prices(mocker):
    fake_history = pd.DataFrame(
        {"Open": [10.0], "High": [11.0], "Low": [9.5], "Close": [10.5], "Volume": [1000]},
        index=pd.to_datetime([date(2024, 1, 2)]),
    )
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = fake_history
    mock_ticker.fast_info = {"currency": "USD"}
    mocker.patch("stock_tracker.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker)

    provider = YFinanceProvider()
    bars = provider.get_prices("AAPL", date(2024, 1, 2), date(2024, 1, 2))

    assert len(bars) == 1
    assert bars[0].close == 10.5
    assert bars[0].volume == 1000
    assert bars[0].currency == "USD"


def test_yfinance_provider_skips_rows_with_missing_ohlc(mocker):
    fake_history = pd.DataFrame(
        {
            "Open": [10.0, float("nan")],
            "High": [11.0, float("nan")],
            "Low": [9.5, float("nan")],
            "Close": [10.5, float("nan")],
            "Volume": [1000, 0],
        },
        index=pd.to_datetime([date(2024, 1, 2), date(2024, 1, 3)]),
    )
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = fake_history
    mock_ticker.fast_info = {"currency": "USD"}
    mocker.patch("stock_tracker.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker)

    provider = YFinanceProvider()
    bars = provider.get_prices("EBAY", date(2024, 1, 2), date(2024, 1, 3))

    assert len(bars) == 1
    assert bars[0].date == date(2024, 1, 2)


def test_yfinance_provider_empty_history_returns_empty_list(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()
    mocker.patch("stock_tracker.providers.yfinance_provider.yf.Ticker", return_value=mock_ticker)

    provider = YFinanceProvider()
    assert provider.get_prices("AAPL", date(2024, 1, 2), date(2024, 1, 2)) == []


def test_fmp_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    with pytest.raises(ValueError):
        FMPProvider(api_key=None)


def test_fmp_provider_get_fundamentals(mocker):
    def fake_get(self, path, **params):
        if path.startswith("income-statement"):
            return [
                {
                    "date": "2024-03-31",
                    "fillingDate": "2024-05-01",
                    "revenue": 1000,
                    "ebitda": 200,
                }
            ]
        if path.startswith("cash-flow-statement"):
            return [{"date": "2024-03-31", "freeCashFlow": 150}]
        if path.startswith("balance-sheet-statement"):
            return [{"date": "2024-03-31", "netDebt": 50}]
        if path.startswith("enterprise-values"):
            return [{"date": "2024-03-31", "marketCapitalization": 5000}]
        return []

    mocker.patch.object(FMPProvider, "_get", fake_get)
    provider = FMPProvider(api_key="test-key")

    snapshots = provider.get_fundamentals("AAPL")

    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.period_end == date(2024, 3, 31)
    assert snap.report_date == date(2024, 5, 1)
    assert snap.revenue == 1000
    assert snap.free_cash_flow == 150
    assert snap.net_debt == 50
    assert snap.ebitda == 200
    assert snap.market_cap == 5000


def test_fmp_provider_get_fundamentals_skips_rows_missing_dates(mocker):
    def fake_get(self, path, **params):
        if path.startswith("income-statement"):
            return [{"revenue": 1000}]
        return []

    mocker.patch.object(FMPProvider, "_get", fake_get)
    provider = FMPProvider(api_key="test-key")
    assert provider.get_fundamentals("AAPL") == []


def test_fmp_provider_get_estimates(mocker):
    def fake_get(self, path, **params):
        return [{"date": "2025-12-31", "estimatedEpsAvg": 6.5}]

    mocker.patch.object(FMPProvider, "_get", fake_get)
    provider = FMPProvider(api_key="test-key")

    estimates = provider.get_estimates("AAPL")

    assert len(estimates) == 1
    assert estimates[0].fiscal_year == 2025
    assert estimates[0].eps_estimate == 6.5
    assert estimates[0].as_of_date == date.today()


def test_fmp_provider_get_earnings_calendar(mocker):
    def fake_get(self, path, **params):
        return [{"date": "2024-04-25", "eps": 1.5, "epsEstimated": 1.4}]

    mocker.patch.object(FMPProvider, "_get", fake_get)
    provider = FMPProvider(api_key="test-key")

    events = provider.get_earnings_calendar("AAPL")

    assert len(events) == 1
    assert events[0].earnings_date == date(2024, 4, 25)
    assert events[0].confirmed is True
