from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from stock_tracker.db.models import PriceDaily, Ticker
from stock_tracker.db.session import get_session


def test_insert_and_query_ticker(db):
    with get_session() as session:
        session.add(
            Ticker(
                ticker="AAPL",
                name="Apple",
                exchange="US",
                currency="USD",
                sector="Technology",
                index_membership="SP500",
            )
        )
    with get_session() as session:
        row = session.query(Ticker).filter_by(ticker="AAPL").one()
        assert row.name == "Apple"
        assert row.active is True
        assert row.added_date == date.today()


def test_price_daily_unique_constraint(db):
    with get_session() as session:
        session.add(
            Ticker(
                ticker="AAPL",
                name="Apple",
                exchange="US",
                currency="USD",
                index_membership="SP500",
            )
        )
    with get_session() as session:
        session.add(
            PriceDaily(
                ticker="AAPL",
                date=date(2024, 1, 2),
                open=1,
                high=2,
                low=0.5,
                close=1.5,
                volume=100,
                close_eur=1.4,
            )
        )
    with get_session() as session:
        row = session.query(PriceDaily).filter_by(ticker="AAPL").one()
        assert row.close == 1.5

    with pytest.raises(IntegrityError):
        with get_session() as session:
            session.add(
                PriceDaily(
                    ticker="AAPL",
                    date=date(2024, 1, 2),
                    open=1,
                    high=2,
                    low=0.5,
                    close=1.5,
                    volume=100,
                    close_eur=1.4,
                )
            )
