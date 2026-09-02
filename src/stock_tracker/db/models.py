"""SQLAlchemy ORM models for the stock-tracker database.

Tables beyond Module 1's immediate scope (`signals`, `trades_paper`, `journal`)
are defined here too since the spec lists them as part of the Module 1 schema;
they stay empty until the modules that populate them (3, 7) are built.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Ticker(Base):
    __tablename__ = "tickers"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    exchange: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    sector: Mapped[str | None] = mapped_column(String, nullable=True)
    index_membership: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    added_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)

    prices: Mapped[list[PriceDaily]] = relationship(back_populates="ticker_ref")


class FxRate(Base):
    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("date", "currency", name="uq_fx_rates_date_currency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    rate_to_eur: Mapped[float] = mapped_column(Float, nullable=False)


class PriceDaily(Base):
    __tablename__ = "prices_daily"
    __table_args__ = (UniqueConstraint("ticker", "date", name="uq_prices_daily_ticker_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    close_eur: Mapped[float] = mapped_column(Float, nullable=False)

    ticker_ref: Mapped[Ticker] = relationship(back_populates="prices")


class Fundamentals(Base):
    __tablename__ = "fundamentals"
    __table_args__ = (
        UniqueConstraint("ticker", "period_end", name="uq_fundamentals_ticker_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    free_cash_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_debt: Mapped[float | None] = mapped_column(Float, nullable=True)
    ebitda: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)


class AnalystEstimate(Base):
    __tablename__ = "analyst_estimates"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "as_of_date", "fiscal_year", name="uq_analyst_estimates_ticker_date_fy"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    eps_estimate: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)


class EarningsCalendar(Base):
    __tablename__ = "earnings_calendar"
    __table_args__ = (
        UniqueConstraint("ticker", "earnings_date", name="uq_earnings_calendar_ticker_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), nullable=False)
    earnings_date: Mapped[date] = mapped_column(Date, nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "date", "signal_type", "tranche", name="uq_signals_ticker_date_type_tranche"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    tranche: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signal_type: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    sma50: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi14: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_52w_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)


class TradePaper(Base):
    __tablename__ = "trades_paper"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), nullable=False)
    tranche: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)


class Journal(Base):
    __tablename__ = "journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades_paper.id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    signal_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expectation: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
