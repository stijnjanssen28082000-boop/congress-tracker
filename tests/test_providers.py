from datetime import date, timedelta

import pandas as pd
import pytest

from stock_tracker.providers.fmp_provider import FMPProvider
from stock_tracker.providers.yfinance_fundamentals_provider import YFinanceFundamentalsProvider
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


def _mock_yf_fundamentals_ticker(mocker, income=None, balance=None, cashflow=None, market_cap=None):
    mock_ticker = mocker.MagicMock()
    mock_ticker.quarterly_income_stmt = income if income is not None else pd.DataFrame()
    mock_ticker.quarterly_balance_sheet = balance if balance is not None else pd.DataFrame()
    mock_ticker.quarterly_cashflow = cashflow if cashflow is not None else pd.DataFrame()
    mock_ticker.info = {}
    mock_ticker.fast_info = {"market_cap": market_cap}
    mocker.patch(
        "stock_tracker.providers.yfinance_fundamentals_provider.yf.Ticker",
        return_value=mock_ticker,
    )
    return mock_ticker


def test_yfinance_fundamentals_provider_get_fundamentals_direct_rows(mocker):
    col = pd.Timestamp("2024-03-31")
    income = pd.DataFrame({col: [1000.0, 200.0]}, index=["Total Revenue", "EBITDA"])
    cashflow = pd.DataFrame({col: [50.0]}, index=["Free Cash Flow"])
    balance = pd.DataFrame({col: [30.0]}, index=["Net Debt"])
    _mock_yf_fundamentals_ticker(
        mocker, income=income, balance=balance, cashflow=cashflow, market_cap=5_000_000_000
    )

    provider = YFinanceFundamentalsProvider()
    snapshots = provider.get_fundamentals("AAPL")

    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.period_end == date(2024, 3, 31)
    assert snap.report_date == date(2024, 3, 31) + timedelta(days=45)
    assert snap.revenue == 1000.0
    assert snap.free_cash_flow == 50.0
    assert snap.net_debt == 30.0
    assert snap.ebitda == 200.0
    assert snap.market_cap == 5_000_000_000


def test_yfinance_fundamentals_provider_get_fundamentals_computes_fallbacks(mocker):
    col = pd.Timestamp("2024-03-31")
    income = pd.DataFrame({col: [1000.0]}, index=["Total Revenue"])
    cashflow = pd.DataFrame(
        {col: [80.0, -20.0]}, index=["Operating Cash Flow", "Capital Expenditure"]
    )
    balance = pd.DataFrame(
        {col: [100.0, 40.0]}, index=["Total Debt", "Cash And Cash Equivalents"]
    )
    _mock_yf_fundamentals_ticker(mocker, income=income, balance=balance, cashflow=cashflow)

    provider = YFinanceFundamentalsProvider()
    snapshots = provider.get_fundamentals("AAPL")

    assert len(snapshots) == 1
    assert snapshots[0].free_cash_flow == 60.0  # 80 + (-20)
    assert snapshots[0].net_debt == 60.0  # 100 - 40


class _AttrFastInfo:
    """Stubs yfinance's FastInfo attribute-style access (`.market_cap`)."""

    def __init__(self, market_cap):
        self.market_cap = market_cap


class _CamelCaseDictFastInfo(dict):
    """Stubs FastInfo dict-style access under the camelCase key, with no
    `.market_cap` attribute — forces the accessor chain past attribute
    access and the `["marketCap"]` getitem before landing on `.get(...)`."""


def test_yfinance_fundamentals_provider_market_cap_via_attribute(mocker):
    col = pd.Timestamp("2024-03-31")
    income = pd.DataFrame({col: [1000.0]}, index=["Total Revenue"])
    mock_ticker = mocker.MagicMock()
    mock_ticker.quarterly_income_stmt = income
    mock_ticker.quarterly_balance_sheet = pd.DataFrame()
    mock_ticker.quarterly_cashflow = pd.DataFrame()
    mock_ticker.info = {}
    mock_ticker.fast_info = _AttrFastInfo(7_000_000_000)
    mocker.patch(
        "stock_tracker.providers.yfinance_fundamentals_provider.yf.Ticker",
        return_value=mock_ticker,
    )

    provider = YFinanceFundamentalsProvider()
    snapshots = provider.get_fundamentals("AAPL")

    assert snapshots[0].market_cap == 7_000_000_000


def test_yfinance_fundamentals_provider_market_cap_via_camelcase_key(mocker):
    col = pd.Timestamp("2024-03-31")
    income = pd.DataFrame({col: [1000.0]}, index=["Total Revenue"])
    mock_ticker = mocker.MagicMock()
    mock_ticker.quarterly_income_stmt = income
    mock_ticker.quarterly_balance_sheet = pd.DataFrame()
    mock_ticker.quarterly_cashflow = pd.DataFrame()
    mock_ticker.info = {}
    mock_ticker.fast_info = _CamelCaseDictFastInfo(marketCap=8_000_000_000)
    mocker.patch(
        "stock_tracker.providers.yfinance_fundamentals_provider.yf.Ticker",
        return_value=mock_ticker,
    )

    provider = YFinanceFundamentalsProvider()
    snapshots = provider.get_fundamentals("AAPL")

    assert snapshots[0].market_cap == 8_000_000_000


def test_yfinance_fundamentals_provider_market_cap_falls_back_to_shares_times_price(mocker):
    col = pd.Timestamp("2024-03-31")
    income = pd.DataFrame({col: [1000.0]}, index=["Total Revenue"])
    mock_ticker = mocker.MagicMock()
    mock_ticker.quarterly_income_stmt = income
    mock_ticker.quarterly_balance_sheet = pd.DataFrame()
    mock_ticker.quarterly_cashflow = pd.DataFrame()
    mock_ticker.info = {}
    mock_ticker.fast_info = {"shares": 1000.0, "lastPrice": 50.0}
    mocker.patch(
        "stock_tracker.providers.yfinance_fundamentals_provider.yf.Ticker",
        return_value=mock_ticker,
    )

    provider = YFinanceFundamentalsProvider()
    snapshots = provider.get_fundamentals("AAPL")

    assert snapshots[0].market_cap == 50_000.0


def test_yfinance_fundamentals_provider_market_cap_none_when_unavailable(mocker):
    col = pd.Timestamp("2024-03-31")
    income = pd.DataFrame({col: [1000.0]}, index=["Total Revenue"])
    mock_ticker = mocker.MagicMock()
    mock_ticker.quarterly_income_stmt = income
    mock_ticker.quarterly_balance_sheet = pd.DataFrame()
    mock_ticker.quarterly_cashflow = pd.DataFrame()
    mock_ticker.info = {}
    mock_ticker.fast_info = {}
    mocker.patch(
        "stock_tracker.providers.yfinance_fundamentals_provider.yf.Ticker",
        return_value=mock_ticker,
    )

    provider = YFinanceFundamentalsProvider()
    snapshots = provider.get_fundamentals("AAPL")

    assert snapshots[0].market_cap is None


def test_yfinance_fundamentals_provider_market_cap_prefers_info_over_fast_info(mocker):
    col = pd.Timestamp("2024-03-31")
    income = pd.DataFrame({col: [1000.0]}, index=["Total Revenue"])
    mock_ticker = mocker.MagicMock()
    mock_ticker.quarterly_income_stmt = income
    mock_ticker.quarterly_balance_sheet = pd.DataFrame()
    mock_ticker.quarterly_cashflow = pd.DataFrame()
    mock_ticker.info = {"marketCap": 9_000_000_000}
    # fast_info has a different (stale/unreliable) value — .info should win.
    mock_ticker.fast_info = {"market_cap": 1_000_000_000}
    mocker.patch(
        "stock_tracker.providers.yfinance_fundamentals_provider.yf.Ticker",
        return_value=mock_ticker,
    )

    provider = YFinanceFundamentalsProvider()
    snapshots = provider.get_fundamentals("AAPL")

    assert snapshots[0].market_cap == 9_000_000_000


def test_yfinance_fundamentals_provider_get_fundamentals_empty_returns_empty(mocker):
    _mock_yf_fundamentals_ticker(mocker)
    provider = YFinanceFundamentalsProvider()
    assert provider.get_fundamentals("AAPL") == []


def test_yfinance_fundamentals_provider_get_estimates(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.earnings_estimate = pd.DataFrame(
        {"avg": [5.0, 6.0, 1.0]}, index=["0y", "+1y", "0q"]
    )
    mocker.patch(
        "stock_tracker.providers.yfinance_fundamentals_provider.yf.Ticker",
        return_value=mock_ticker,
    )

    provider = YFinanceFundamentalsProvider()
    estimates = provider.get_estimates("AAPL")

    today = date.today()
    assert {(e.fiscal_year, e.eps_estimate) for e in estimates} == {
        (today.year, 5.0),
        (today.year + 1, 6.0),
    }
    assert all(e.as_of_date == today for e in estimates)


def test_yfinance_fundamentals_provider_get_estimates_empty_table(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.earnings_estimate = pd.DataFrame()
    mocker.patch(
        "stock_tracker.providers.yfinance_fundamentals_provider.yf.Ticker",
        return_value=mock_ticker,
    )
    provider = YFinanceFundamentalsProvider()
    assert provider.get_estimates("AAPL") == []


def test_yfinance_fundamentals_provider_get_earnings_calendar(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.get_earnings_dates.return_value = pd.DataFrame(
        {"EPS Estimate": [1.5, 1.6]},
        index=pd.to_datetime([date(2024, 4, 25), date(2024, 7, 25)]),
    )
    mocker.patch(
        "stock_tracker.providers.yfinance_fundamentals_provider.yf.Ticker",
        return_value=mock_ticker,
    )

    provider = YFinanceFundamentalsProvider()
    events = provider.get_earnings_calendar("AAPL")

    assert [e.earnings_date for e in events] == [date(2024, 4, 25), date(2024, 7, 25)]
    assert all(e.confirmed for e in events)


def test_yfinance_fundamentals_provider_get_earnings_calendar_empty(mocker):
    mock_ticker = mocker.MagicMock()
    mock_ticker.get_earnings_dates.return_value = pd.DataFrame()
    mocker.patch(
        "stock_tracker.providers.yfinance_fundamentals_provider.yf.Ticker",
        return_value=mock_ticker,
    )
    provider = YFinanceFundamentalsProvider()
    assert provider.get_earnings_calendar("AAPL") == []


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
