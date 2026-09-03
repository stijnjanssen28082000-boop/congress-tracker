from datetime import date, timedelta

import pytest

from stock_tracker import ingest
from stock_tracker.db.models import AnalystEstimate, EarningsCalendar, FxRate, PriceDaily, Ticker
from stock_tracker.db.session import get_session
from stock_tracker.providers.base import (
    EarningsEvent,
    EstimateSnapshot,
    FundamentalsSnapshot,
    PriceBar,
)


class FakePriceProvider:
    def __init__(self, bars_by_ticker):
        self.bars_by_ticker = bars_by_ticker
        self.calls = []

    def get_prices(self, ticker, start, end):
        self.calls.append((ticker, start, end))
        return self.bars_by_ticker.get(ticker, [])


class FakeFundamentalsProvider:
    def __init__(self):
        self.fundamentals_calls = []
        self.estimate_calls = []
        self.earnings_calls = []

    def get_fundamentals(self, ticker):
        self.fundamentals_calls.append(ticker)
        return [
            FundamentalsSnapshot(
                period_end=date(2024, 3, 31),
                report_date=date(2024, 5, 1),
                revenue=100,
                free_cash_flow=10,
                net_debt=5,
                ebitda=20,
                market_cap=1000,
            )
        ]

    def get_estimates(self, ticker):
        self.estimate_calls.append(ticker)
        return [EstimateSnapshot(as_of_date=date.today(), fiscal_year=2025, eps_estimate=5.0)]

    def get_earnings_calendar(self, ticker):
        self.earnings_calls.append(ticker)
        return [EarningsEvent(earnings_date=date.today() + timedelta(days=10), confirmed=True)]


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


def test_run_full_stores_prices_and_fundamentals(db, mocker):
    _seed_ticker()
    mocker.patch("stock_tracker.ingest.build_universe", return_value=[])
    mocker.patch("stock_tracker.ingest.sync_universe_to_db", return_value=0)
    end = date.today() - timedelta(days=1)
    bars = [PriceBar(date=end, open=1, high=2, low=0.5, close=1.5, volume=100, currency="EUR")]
    mocker.patch(
        "stock_tracker.ingest.YFinanceProvider",
        return_value=FakePriceProvider({"AAPL": bars}),
    )
    fake_fundamentals = FakeFundamentalsProvider()
    mocker.patch(
        "stock_tracker.ingest._build_fundamentals_provider", return_value=fake_fundamentals
    )

    ingest.run_full(db)

    with get_session() as session:
        price = session.query(PriceDaily).filter_by(ticker="AAPL").one()
        assert price.close == 1.5
        assert price.close_eur == 1.5  # EUR ticker: no conversion
    assert fake_fundamentals.fundamentals_calls == ["AAPL"]
    assert fake_fundamentals.estimate_calls == ["AAPL"]
    assert fake_fundamentals.earnings_calls == ["AAPL"]


def test_run_full_skips_fundamentals_without_provider(db, mocker):
    _seed_ticker()
    mocker.patch("stock_tracker.ingest.build_universe", return_value=[])
    mocker.patch("stock_tracker.ingest.sync_universe_to_db", return_value=0)
    mocker.patch("stock_tracker.ingest.YFinanceProvider", return_value=FakePriceProvider({}))
    mocker.patch("stock_tracker.ingest._build_fundamentals_provider", return_value=None)

    ingest.run_full(db)  # should not raise


def test_run_daily_only_fetches_since_last_known_date(db, mocker):
    _seed_ticker()
    with get_session() as session:
        session.add(
            PriceDaily(
                ticker="AAPL",
                date=date(2024, 1, 2),
                open=1,
                high=1,
                low=1,
                close=1,
                volume=1,
                close_eur=1,
            )
        )
    mocker.patch("stock_tracker.ingest.build_universe", return_value=[])
    mocker.patch("stock_tracker.ingest.sync_universe_to_db", return_value=0)
    fake_provider = FakePriceProvider({"AAPL": []})
    mocker.patch("stock_tracker.ingest.YFinanceProvider", return_value=fake_provider)
    mocker.patch("stock_tracker.ingest._build_fundamentals_provider", return_value=None)

    ingest.run_daily(db)

    assert len(fake_provider.calls) == 1
    _, start, _end = fake_provider.calls[0]
    assert start == date(2024, 1, 3)


def test_run_daily_uses_fallback_start_when_no_prior_data(db, mocker):
    _seed_ticker()
    mocker.patch("stock_tracker.ingest.build_universe", return_value=[])
    mocker.patch("stock_tracker.ingest.sync_universe_to_db", return_value=0)
    fake_provider = FakePriceProvider({"AAPL": []})
    mocker.patch("stock_tracker.ingest.YFinanceProvider", return_value=fake_provider)
    mocker.patch("stock_tracker.ingest._build_fundamentals_provider", return_value=None)

    ingest.run_daily(db)

    _, start, end = fake_provider.calls[0]
    assert start == end.replace(year=end.year - db.ingest.full_history_years)


def test_store_earnings_dedupes_within_same_batch(db):
    _seed_ticker()
    same_date = date.today() + timedelta(days=10)
    events = [
        EarningsEvent(earnings_date=same_date, confirmed=True),
        EarningsEvent(earnings_date=same_date, confirmed=True),
    ]

    stored = ingest._store_earnings("AAPL", events, source="yfinance")

    assert stored == 1
    with get_session() as session:
        assert session.query(EarningsCalendar).filter_by(ticker="AAPL").count() == 1


def test_run_full_stores_configured_provider_as_source(db, mocker):
    _seed_ticker()
    mocker.patch("stock_tracker.ingest.build_universe", return_value=[])
    mocker.patch("stock_tracker.ingest.sync_universe_to_db", return_value=0)
    mocker.patch("stock_tracker.ingest.YFinanceProvider", return_value=FakePriceProvider({}))
    mocker.patch(
        "stock_tracker.ingest._build_fundamentals_provider",
        return_value=FakeFundamentalsProvider(),
    )
    assert db.ingest.fundamentals_provider == "yfinance"

    ingest.run_full(db)

    with get_session() as session:
        estimate = session.query(AnalystEstimate).filter_by(ticker="AAPL").one()
        earnings = session.query(EarningsCalendar).filter_by(ticker="AAPL").one()
        assert estimate.source == "yfinance"
        assert earnings.source == "yfinance"


def test_prune_old_prices_deletes_old_rows_keeps_recent(db):
    _seed_ticker(currency="USD")
    old_date = date.today() - timedelta(days=365 * db.ingest.price_retention_years + 30)
    recent_date = date.today() - timedelta(days=1)
    with get_session() as session:
        session.add_all(
            [
                PriceDaily(
                    ticker="AAPL",
                    date=old_date,
                    open=1,
                    high=1,
                    low=1,
                    close=1,
                    volume=1,
                    close_eur=1,
                ),
                PriceDaily(
                    ticker="AAPL",
                    date=recent_date,
                    open=2,
                    high=2,
                    low=2,
                    close=2,
                    volume=2,
                    close_eur=2,
                ),
                FxRate(date=old_date, currency="USD", rate_to_eur=0.9),
                FxRate(date=recent_date, currency="USD", rate_to_eur=0.95),
            ]
        )

    result = ingest.prune_old_prices(db)

    assert result == {"prices_deleted": 1, "fx_deleted": 1}
    with get_session() as session:
        remaining_price_dates = {d for (d,) in session.query(PriceDaily.date).all()}
        remaining_fx_dates = {d for (d,) in session.query(FxRate.date).all()}
    assert remaining_price_dates == {recent_date}
    assert remaining_fx_dates == {recent_date}


def test_build_fundamentals_provider_yfinance(db):
    provider = ingest._build_fundamentals_provider(db)
    assert isinstance(provider, ingest.YFinanceFundamentalsProvider)


def test_build_fundamentals_provider_fmp(db, monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    db._data["ingest"]["fundamentals_provider"] = "fmp"
    provider = ingest._build_fundamentals_provider(db)
    assert isinstance(provider, ingest.FMPProvider)


def test_build_fundamentals_provider_fmp_without_key_returns_none(db, monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    db._data["ingest"]["fundamentals_provider"] = "fmp"
    assert ingest._build_fundamentals_provider(db) is None


def test_build_fundamentals_provider_rejects_unknown_name(db):
    db._data["ingest"]["fundamentals_provider"] = "bogus"
    with pytest.raises(ValueError):
        ingest._build_fundamentals_provider(db)


def test_main_requires_full_or_daily_flag():
    with pytest.raises(SystemExit):
        ingest.main([])


def test_main_dispatches_to_run_full(mocker):
    mock_config = mocker.MagicMock()
    mocker.patch("stock_tracker.ingest.load_dotenv")
    mocker.patch("stock_tracker.ingest.load_config", return_value=mock_config)
    mocker.patch("stock_tracker.ingest.setup_logging")
    mock_run_full = mocker.patch("stock_tracker.ingest.run_full")
    mock_run_daily = mocker.patch("stock_tracker.ingest.run_daily")

    ingest.main(["--full"])

    mock_run_full.assert_called_once_with(mock_config)
    mock_run_daily.assert_not_called()


def test_main_dispatches_to_run_daily(mocker):
    mock_config = mocker.MagicMock()
    mocker.patch("stock_tracker.ingest.load_dotenv")
    mocker.patch("stock_tracker.ingest.load_config", return_value=mock_config)
    mocker.patch("stock_tracker.ingest.setup_logging")
    mock_run_full = mocker.patch("stock_tracker.ingest.run_full")
    mock_run_daily = mocker.patch("stock_tracker.ingest.run_daily")

    ingest.main(["--daily"])

    mock_run_daily.assert_called_once_with(mock_config)
    mock_run_full.assert_not_called()
