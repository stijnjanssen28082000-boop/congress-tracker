from datetime import date, timedelta

import app
from stock_tracker.db.models import (
    EarningsCalendar,
    PriceDaily,
    QualityScore,
    Signal,
    Ticker,
    TradePaper,
)
from stock_tracker.db.session import get_session

AS_OF = date(2024, 6, 3)


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


def _seed_eligible(ticker, as_of):
    with get_session() as session:
        session.add(
            QualityScore(
                ticker=ticker,
                date=as_of,
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


def _seed_prices(ticker, as_of, num_days, baseline, today_close):
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
                    volume=1_000_000,
                    close_eur=close,
                )
            )


def test_load_watchlist_shows_eligible_ticker_with_active_tranche(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF)
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=70)

    df = app.load_watchlist(AS_OF, db)

    assert list(df["Ticker"]) == ["AAPL"]
    assert df.iloc[0]["Actief signaal"] == "Tranche 3"


def test_load_watchlist_includes_next_earnings_date(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF)
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=70)
    with get_session() as session:
        session.add(
            EarningsCalendar(
                ticker="AAPL", earnings_date=date(2024, 6, 20), confirmed=True, source="fmp"
            )
        )

    df = app.load_watchlist(AS_OF, db)

    assert df.iloc[0]["Volgende earnings"] == date(2024, 6, 20)


def test_load_watchlist_empty_without_eligible_tickers(db):
    _seed_ticker()
    df = app.load_watchlist(AS_OF, db)
    assert df.empty


def test_load_watchlist_marks_full_pass_as_volledig(db):
    _seed_ticker()
    _seed_eligible("AAPL", AS_OF)
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=70)

    df = app.load_watchlist(AS_OF, db)

    assert df.iloc[0]["Status"] == "Volledig (6/6)"


def test_load_watchlist_marks_grace_period_tickers_as_voorlopig(db):
    _seed_ticker()
    with get_session() as session:
        session.add(
            QualityScore(
                ticker="AAPL",
                date=AS_OF,
                score=5,
                eligible=True,
                market_cap_pass=True,
                fcf_positive_pass=True,
                revenue_growth_pass=True,
                net_debt_ebitda_pass=True,
                eps_estimate_trend_pass=False,
                eps_estimate_current=6.0,
                eps_estimate_prior=None,
                volume_pass=True,
            )
        )
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=70)

    df = app.load_watchlist(AS_OF, db)

    assert df.iloc[0]["Status"] == "Voorlopig (5/6 — EPS-trend nog niet meetbaar)"


def test_load_signals_for_date(db):
    _seed_ticker()
    with get_session() as session:
        session.add(
            Signal(
                ticker="AAPL",
                date=AS_OF,
                tranche=2,
                signal_type="ENTRY",
                price=84.0,
                sma50=99.0,
                rsi14=30.0,
            )
        )

    df = app.load_signals_for_date(AS_OF)

    assert len(df) == 1
    assert df.iloc[0]["Ticker"] == "AAPL"
    assert df.iloc[0]["Bedrijf"] == "Test Co"
    assert df.iloc[0]["Tranche"] == 2


def test_load_signals_for_date_includes_week_change_pct(db):
    _seed_ticker()
    _seed_prices("AAPL", AS_OF, 10, baseline=100, today_close=84)
    with get_session() as session:
        session.add(
            Signal(
                ticker="AAPL", date=AS_OF, tranche=2, signal_type="ENTRY", price=84.0, sma50=99.0
            )
        )

    df = app.load_signals_for_date(AS_OF)

    assert df.iloc[0]["Verandering 1 week %"] == -16.0


def test_load_signals_for_date_week_change_pct_none_without_history(db):
    _seed_ticker()
    with get_session() as session:
        session.add(
            Signal(
                ticker="AAPL", date=AS_OF, tranche=2, signal_type="ENTRY", price=84.0, sma50=99.0
            )
        )

    df = app.load_signals_for_date(AS_OF)

    assert df.iloc[0]["Verandering 1 week %"] is None


def test_load_signals_for_date_orders_deepest_tranche_first(db):
    _seed_ticker()
    with get_session() as session:
        session.add(
            Signal(
                ticker="AAPL", date=AS_OF, tranche=1, signal_type="ENTRY", price=90.0, sma50=99.0
            )
        )
        session.add(
            Signal(
                ticker="AAPL", date=AS_OF, tranche=3, signal_type="ENTRY", price=70.0, sma50=99.0
            )
        )

    df = app.load_signals_for_date(AS_OF)

    assert list(df["Tranche"]) == [3, 1]


def test_load_paper_portfolio_computes_unrealized_and_flags(db):
    _seed_ticker()
    _seed_prices("AAPL", AS_OF, 60, baseline=100, today_close=120)
    with get_session() as session:
        session.add(
            TradePaper(
                ticker="AAPL",
                tranche=1,
                entry_date=AS_OF - timedelta(days=10),
                entry_price=100.0,
                size=2000.0,
                status="OPEN",
            )
        )
        session.add(
            Signal(
                ticker="AAPL",
                date=AS_OF,
                tranche=1,
                signal_type="REVIEW",
                price=100.0,
                notes="test",
            )
        )

    df = app.load_paper_portfolio()

    assert len(df) == 1
    row = df.iloc[0]
    assert row["Huidige prijs"] == 120
    assert row["Ongerealiseerd %"] == 20.0
    assert row["Flag"] == "REVIEW"


def test_load_paper_portfolio_empty_without_open_trades(db):
    assert app.load_paper_portfolio().empty


def test_journal_round_trip(db):
    _seed_ticker()
    with get_session() as session:
        session.add(
            TradePaper(
                ticker="AAPL",
                tranche=1,
                entry_date=AS_OF,
                entry_price=91.0,
                size=2000.0,
                status="OPEN",
            )
        )
        session.add(
            Signal(
                ticker="AAPL",
                date=AS_OF,
                tranche=1,
                signal_type="ENTRY",
                price=91.0,
                sma50=99.0,
                rsi14=20.0,
                distance_52w_high=-9.0,
            )
        )

    trades_df = app.load_recent_trades()
    trade_id = int(trades_df.iloc[0]["id"])

    app.save_journal_entry(trade_id, "dip in kwaliteitsnaam", "herstel binnen 12 weken")

    entries = app.load_journal_entries()
    assert len(entries) == 1
    entry_id = int(entries.iloc[0]["id"])
    assert entries.iloc[0]["Trade"] == "AAPL T1"
    assert entries.iloc[0]["Reden"] == "dip in kwaliteitsnaam"
    assert entries.iloc[0]["Uitkomst"] is None

    with get_session() as session:
        from stock_tracker.db.models import Journal

        stored = session.query(Journal).filter_by(id=entry_id).one()
        assert stored.signal_values["signal_price"] == 91.0
        assert stored.signal_values["sma50"] == 99.0

    app.update_journal_outcome(entry_id, "winst genomen bij +12%")

    entries_after = app.load_journal_entries()
    assert entries_after.iloc[0]["Uitkomst"] == "winst genomen bij +12%"


def test_latest_signal_date_returns_most_recent(db):
    _seed_ticker()
    with get_session() as session:
        session.add(
            Signal(
                ticker="AAPL",
                date=date(2024, 5, 1),
                tranche=1,
                signal_type="ENTRY",
                price=90.0,
            )
        )
        session.add(
            Signal(
                ticker="AAPL",
                date=AS_OF,
                tranche=1,
                signal_type="ENTRY",
                price=90.0,
            )
        )

    assert app.latest_signal_date() == AS_OF


def test_latest_signal_date_none_when_empty(db):
    assert app.latest_signal_date() is None


def test_latest_price_date_returns_most_recent(db):
    _seed_ticker()
    _seed_prices("AAPL", AS_OF, 5, baseline=100, today_close=100)
    assert app.latest_price_date() == AS_OF


def test_latest_price_date_none_when_empty(db):
    assert app.latest_price_date() is None
