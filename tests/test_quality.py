from datetime import date, timedelta

import pytest

from stock_tracker import quality
from stock_tracker.config import load_config
from stock_tracker.db.models import AnalystEstimate, Fundamentals, PriceDaily, QualityScore, Ticker
from stock_tracker.db.session import get_session

AS_OF = date(2024, 6, 1)

# 8 quarters, most recent first, revenue TTM growing ~23% YoY, positive FCF,
# comfortable net-debt/EBITDA, market cap above the default 30bn EUR threshold.
# Columns: period_end, report_date, revenue, free_cash_flow, net_debt, ebitda, market_cap
_FUNDAMENTALS_ROWS = [
    (date(2024, 3, 31), date(2024, 5, 1), 1100, 50, 200, 300, 40_000_000_000),
    (date(2023, 12, 31), date(2024, 2, 1), 1080, 45, 210, 290, 39_000_000_000),
    (date(2023, 9, 30), date(2023, 11, 1), 1050, 40, 220, 280, 38_000_000_000),
    (date(2023, 6, 30), date(2023, 8, 1), 1000, 35, 230, 270, 37_000_000_000),
    (date(2023, 3, 31), date(2023, 5, 1), 900, 20, 240, 250, 35_000_000_000),
    (date(2022, 12, 31), date(2023, 2, 1), 880, 18, 250, 240, 34_000_000_000),
    (date(2022, 9, 30), date(2022, 11, 1), 850, 15, 260, 230, 33_000_000_000),
    (date(2022, 6, 30), date(2022, 8, 1), 800, 10, 270, 220, 32_000_000_000),
]
FUNDAMENTALS_ALL_PASS = [
    dict(
        period_end=period_end,
        report_date=report_date,
        revenue=revenue,
        free_cash_flow=fcf,
        net_debt=net_debt,
        ebitda=ebitda,
        market_cap=market_cap,
    )
    for period_end, report_date, revenue, fcf, net_debt, ebitda, market_cap in _FUNDAMENTALS_ROWS
]


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


def _seed_fundamentals(ticker, rows):
    with get_session() as session:
        for row in rows:
            session.add(Fundamentals(ticker=ticker, source="fmp", **row))


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


def _seed_prices(ticker, as_of, num_days, volume):
    with get_session() as session:
        for i in range(num_days):
            d = as_of - timedelta(days=i)
            session.add(
                PriceDaily(
                    ticker=ticker,
                    date=d,
                    open=100,
                    high=101,
                    low=99,
                    close=100,
                    volume=volume,
                    close_eur=100,
                )
            )


def _seed_all_pass(ticker="AAPL"):
    _seed_ticker(ticker)
    _seed_fundamentals(ticker, FUNDAMENTALS_ALL_PASS)
    _seed_estimate(ticker, date(2024, 1, 10), 2025, 5.0)
    _seed_estimate(ticker, date(2024, 5, 15), 2025, 6.0)
    _seed_prices(ticker, AS_OF, 60, 1_000_000)


def _compute(ticker="AAPL", as_of=AS_OF, config=None):
    config = config or load_config()
    with get_session() as session:
        ticker_row = session.query(Ticker).filter_by(ticker=ticker).one()
        return quality.compute_quality(session, ticker_row, as_of, config)


def test_all_criteria_pass_yields_eligible(db):
    _seed_all_pass()
    result = _compute()
    assert result is not None
    assert result.market_cap_pass
    assert result.fcf_positive_pass
    assert result.revenue_growth_pass
    assert result.net_debt_ebitda_pass
    assert result.eps_estimate_trend_pass
    assert result.volume_pass
    assert result.score == 6
    assert result.eligible


def test_no_fundamentals_returns_none(db):
    _seed_ticker()
    assert _compute() is None


def test_future_fundamentals_are_not_looked_ahead(db):
    _seed_ticker()
    # Only a report filed after AS_OF exists — point-in-time must ignore it.
    _seed_fundamentals(
        "AAPL",
        [
            dict(
                period_end=date(2024, 3, 31),
                report_date=AS_OF + timedelta(days=1),
                revenue=1000,
                free_cash_flow=10,
                net_debt=100,
                ebitda=200,
                market_cap=40_000_000_000,
            )
        ],
    )
    assert _compute() is None


def test_market_cap_below_threshold_fails(db):
    _seed_all_pass()
    with get_session() as session:
        session.query(Fundamentals).filter_by(
            ticker="AAPL", period_end=date(2024, 3, 31)
        ).one().market_cap = 20_000_000_000

    result = _compute()
    assert result.market_cap_pass is False
    assert result.eligible is False


def test_fcf_requires_four_positive_quarters(db):
    _seed_all_pass()
    with get_session() as session:
        session.query(Fundamentals).filter_by(
            ticker="AAPL", period_end=date(2024, 3, 31)
        ).one().free_cash_flow = -5

    result = _compute()
    assert result.fcf_positive_pass is False
    assert result.eligible is False


def test_revenue_growth_requires_eight_quarters(db):
    _seed_ticker()
    _seed_fundamentals("AAPL", FUNDAMENTALS_ALL_PASS[:4])
    _seed_estimate("AAPL", date(2024, 1, 10), 2025, 5.0)
    _seed_estimate("AAPL", date(2024, 5, 15), 2025, 6.0)
    _seed_prices("AAPL", AS_OF, 60, 1_000_000)

    result = _compute()
    assert result.revenue_growth_ttm_pct is None
    assert result.revenue_growth_pass is False


def test_net_debt_to_ebitda_above_threshold_fails(db):
    _seed_all_pass()
    with get_session() as session:
        session.query(Fundamentals).filter_by(
            ticker="AAPL", period_end=date(2024, 3, 31)
        ).one().net_debt = 100_000

    result = _compute()
    assert result.net_debt_ebitda_pass is False


def test_net_cash_passes_net_debt_criterion(db):
    _seed_all_pass()
    with get_session() as session:
        session.query(Fundamentals).filter_by(
            ticker="AAPL", period_end=date(2024, 3, 31)
        ).one().net_debt = -500

    result = _compute()
    assert result.net_debt_ebitda_pass is True


def test_eps_estimate_missing_prior_fails(db):
    _seed_ticker()
    _seed_fundamentals("AAPL", FUNDAMENTALS_ALL_PASS)
    _seed_estimate("AAPL", date(2024, 5, 15), 2025, 6.0)
    _seed_prices("AAPL", AS_OF, 60, 1_000_000)

    result = _compute()
    assert result.eps_estimate_prior is None
    assert result.eps_estimate_trend_pass is False


def test_eps_estimate_flat_or_down_fails(db):
    _seed_ticker()
    _seed_fundamentals("AAPL", FUNDAMENTALS_ALL_PASS)
    _seed_estimate("AAPL", date(2024, 1, 10), 2025, 6.0)
    _seed_estimate("AAPL", date(2024, 5, 15), 2025, 6.0)
    _seed_prices("AAPL", AS_OF, 60, 1_000_000)

    result = _compute()
    assert result.eps_estimate_trend_pass is False


def test_avg_volume_below_threshold_fails(db):
    _seed_ticker()
    _seed_fundamentals("AAPL", FUNDAMENTALS_ALL_PASS)
    _seed_estimate("AAPL", date(2024, 1, 10), 2025, 5.0)
    _seed_estimate("AAPL", date(2024, 5, 15), 2025, 6.0)
    _seed_prices("AAPL", AS_OF, 60, 100)

    result = _compute()
    assert result.volume_pass is False


def test_avg_volume_none_without_prices_fails(db):
    _seed_ticker()
    _seed_fundamentals("AAPL", FUNDAMENTALS_ALL_PASS)

    result = _compute()
    assert result.avg_daily_volume is None
    assert result.volume_pass is False


def test_store_quality_scores_upserts(db):
    _seed_all_pass()
    result = _compute()
    quality.store_quality_scores([result])
    with get_session() as session:
        assert session.query(QualityScore).count() == 1
        row = session.query(QualityScore).one()
        assert row.eligible is True

    downgraded = quality.QualityResult(
        **{**result.__dict__, "market_cap_pass": False},
    )
    quality.store_quality_scores([downgraded])
    with get_session() as session:
        assert session.query(QualityScore).count() == 1
        row = session.query(QualityScore).one()
        assert row.market_cap_pass is False
        assert row.eligible is False


def test_get_eligible_tickers_uses_most_recent_snapshot_on_or_before(db):
    _seed_ticker()
    with get_session() as session:
        session.add(
            QualityScore(
                ticker="AAPL",
                date=date(2024, 5, 6),
                score=6,
                eligible=True,
                market_cap_pass=True,
                fcf_positive_pass=True,
                revenue_growth_pass=True,
                net_debt_ebitda_pass=True,
                eps_estimate_trend_pass=True,
                volume_pass=True,
            )
        )
        session.add(
            QualityScore(
                ticker="AAPL",
                date=date(2024, 5, 13),
                score=5,
                eligible=False,
                market_cap_pass=False,
                fcf_positive_pass=True,
                revenue_growth_pass=True,
                net_debt_ebitda_pass=True,
                eps_estimate_trend_pass=True,
                volume_pass=True,
            )
        )

    assert quality.get_eligible_tickers(date(2024, 5, 10)) == ["AAPL"]
    assert quality.get_eligible_tickers(date(2024, 5, 20)) == []
    assert quality.get_eligible_tickers(date(2024, 5, 1)) == []


def test_run_for_date_stores_scores_for_universe(db):
    _seed_all_pass()
    stored = quality.run_for_date(AS_OF, db)
    assert stored == 1
    with get_session() as session:
        assert session.query(QualityScore).filter_by(ticker="AAPL", date=AS_OF).one().eligible


def test_backfill_only_runs_on_configured_weekday(db, mocker):
    calls = []
    mocker.patch(
        "stock_tracker.quality.run_for_date",
        side_effect=lambda d, c: calls.append(d) or 0,
    )

    total = quality.backfill(date(2024, 5, 1), date(2024, 5, 14), db)  # Mondays: 6, 13

    assert calls == [date(2024, 5, 6), date(2024, 5, 13)]
    assert total == 0


def test_main_requires_both_backfill_bounds():
    with pytest.raises(SystemExit):
        quality.main(["--backfill-start", "2024-01-01"])


def test_main_dispatches_to_run_for_date(mocker):
    mock_config = mocker.MagicMock()
    mocker.patch("stock_tracker.quality.load_config", return_value=mock_config)
    mocker.patch("stock_tracker.quality.setup_logging")
    mocker.patch("stock_tracker.quality.init_db")
    mock_run = mocker.patch("stock_tracker.quality.run_for_date")
    mock_backfill = mocker.patch("stock_tracker.quality.backfill")

    quality.main(["--date", "2024-05-06"])

    mock_run.assert_called_once_with(date(2024, 5, 6), mock_config)
    mock_backfill.assert_not_called()


def test_main_dispatches_to_backfill(mocker):
    mock_config = mocker.MagicMock()
    mocker.patch("stock_tracker.quality.load_config", return_value=mock_config)
    mocker.patch("stock_tracker.quality.setup_logging")
    mocker.patch("stock_tracker.quality.init_db")
    mock_run = mocker.patch("stock_tracker.quality.run_for_date")
    mock_backfill = mocker.patch("stock_tracker.quality.backfill")

    quality.main(["--backfill-start", "2024-05-01", "--backfill-end", "2024-05-14"])

    mock_backfill.assert_called_once_with(date(2024, 5, 1), date(2024, 5, 14), mock_config)
    mock_run.assert_not_called()
