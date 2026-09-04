"""Streamlit dashboard: Watchlist, Signalen vandaag, Portfolio (paper),
Backtest and Journal. Run with `uv run streamlit run app.py`.

Data-loading functions are kept free of `st.*` calls so they're testable in
isolation; all Streamlit rendering lives in `main()`, guarded by
`if __name__ == "__main__"` (which Streamlit satisfies — it runs the script
as `__main__`).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from stock_tracker import backtest, signals
from stock_tracker.config import load_config
from stock_tracker.db.models import EarningsCalendar, Journal, PriceDaily, Ticker, TradePaper
from stock_tracker.db.models import Signal as SignalModel
from stock_tracker.db.session import get_session, init_db
from stock_tracker.quality import get_eligible_tickers, get_eligible_tickers_with_grace

# --- data loading (no Streamlit calls — importable/testable on their own) ----


def load_watchlist(as_of: date, config) -> pd.DataFrame:
    tickers = get_eligible_tickers(as_of)
    grace_by_ticker = get_eligible_tickers_with_grace(as_of)
    rows = []
    with get_session() as session:
        for ticker in tickers:
            ind = signals.compute_indicators(session, ticker, as_of, config)
            if ind is None:
                continue
            next_earnings = (
                session.query(EarningsCalendar)
                .filter(EarningsCalendar.ticker == ticker, EarningsCalendar.earnings_date >= as_of)
                .order_by(EarningsCalendar.earnings_date.asc())
                .first()
            )
            tranche = signals.entry_tranche(ind, config)
            sma_distance_pct = ((ind.close / ind.sma50) - 1) * 100 if ind.sma50 else None
            rows.append(
                {
                    "Ticker": ticker,
                    "Status": (
                        "Voorlopig (5/6 — EPS-trend nog niet meetbaar)"
                        if grace_by_ticker.get(ticker)
                        else "Volledig (6/6)"
                    ),
                    "Koers": round(ind.close, 2),
                    "Afstand SMA50 %": (
                        round(sma_distance_pct, 2) if sma_distance_pct is not None else None
                    ),
                    "Afstand 52w-high %": (
                        round(ind.distance_to_52w_high_pct, 2)
                        if ind.distance_to_52w_high_pct is not None
                        else None
                    ),
                    "RSI(14)": round(ind.rsi14, 1) if ind.rsi14 is not None else None,
                    "Volgende earnings": next_earnings.earnings_date if next_earnings else None,
                    "Actief signaal": f"Tranche {tranche}" if tranche else "-",
                }
            )
    return pd.DataFrame(rows)


def latest_signal_date() -> date | None:
    with get_session() as session:
        row = session.query(SignalModel.date).order_by(SignalModel.date.desc()).first()
    return row[0] if row else None


def latest_price_date() -> date | None:
    with get_session() as session:
        row = session.query(PriceDaily.date).order_by(PriceDaily.date.desc()).first()
    return row[0] if row else None


def load_signals_for_date(as_of: date) -> pd.DataFrame:
    with get_session() as session:
        rows = (
            session.query(SignalModel)
            .filter(SignalModel.date == as_of)
            .order_by(SignalModel.tranche.desc(), SignalModel.signal_type)
            .all()
        )
        names_by_ticker = dict(session.query(Ticker.ticker, Ticker.name).all())
    return pd.DataFrame(
        [
            {
                "Ticker": r.ticker,
                "Bedrijf": names_by_ticker.get(r.ticker, ""),
                "Type": r.signal_type,
                "Tranche": r.tranche,
                "Prijs": r.price,
                "SMA50": r.sma50,
                "RSI14": r.rsi14,
                "Notes": r.notes,
            }
            for r in rows
        ]
    )


def load_paper_portfolio() -> pd.DataFrame:
    today = date.today()
    with get_session() as session:
        open_trades = session.query(TradePaper).filter(TradePaper.status == "OPEN").all()
        rows = []
        for trade in open_trades:
            price_row = (
                session.query(PriceDaily)
                .filter(PriceDaily.ticker == trade.ticker)
                .order_by(PriceDaily.date.desc())
                .first()
            )
            current_price = price_row.close_eur if price_row else None
            unrealized_pct = (
                (current_price / trade.entry_price - 1) * 100 if current_price else None
            )
            flag_row = (
                session.query(SignalModel)
                .filter(
                    SignalModel.ticker == trade.ticker,
                    SignalModel.signal_type.in_(["REVIEW", "EXIT_FUNDAMENTAL"]),
                )
                .order_by(SignalModel.date.desc())
                .first()
            )
            rows.append(
                {
                    "Ticker": trade.ticker,
                    "Tranche": trade.tranche,
                    "Instapdatum": trade.entry_date,
                    "Instapprijs": trade.entry_price,
                    "Huidige prijs": current_price,
                    "Ongerealiseerd %": (
                        round(unrealized_pct, 2) if unrealized_pct is not None else None
                    ),
                    "Dagen open": (today - trade.entry_date).days,
                    "Flag": flag_row.signal_type if flag_row else "-",
                }
            )
    return pd.DataFrame(rows)


def load_recent_trades(limit: int = 100) -> pd.DataFrame:
    with get_session() as session:
        trades = (
            session.query(TradePaper).order_by(TradePaper.entry_date.desc()).limit(limit).all()
        )
    return pd.DataFrame(
        [
            {
                "id": t.id,
                "label": f"#{t.id} {t.ticker} T{t.tranche} ({t.status}, entry {t.entry_date})",
            }
            for t in trades
        ]
    )


def _signal_values_for_trade(session, trade_id: int) -> dict:
    """Snapshots the ENTRY signal's indicator values for this trade, so the
    journal entry captures what the strategy actually saw at entry time."""

    trade = session.query(TradePaper).filter_by(id=trade_id).first()
    if trade is None:
        return {}
    sig = (
        session.query(SignalModel)
        .filter(
            SignalModel.ticker == trade.ticker,
            SignalModel.tranche == trade.tranche,
            SignalModel.signal_type == "ENTRY",
            SignalModel.date <= trade.entry_date,
        )
        .order_by(SignalModel.date.desc())
        .first()
    )
    values = {"entry_price": trade.entry_price}
    if sig is not None:
        values.update(
            {
                "signal_price": sig.price,
                "sma50": sig.sma50,
                "rsi14": sig.rsi14,
                "distance_52w_high": sig.distance_52w_high,
            }
        )
    return values


def save_journal_entry(trade_id: int, reason: str, expectation: str) -> None:
    with get_session() as session:
        signal_values = _signal_values_for_trade(session, trade_id)
        session.add(
            Journal(
                trade_id=trade_id,
                date=date.today(),
                reason=reason or None,
                expectation=expectation or None,
                signal_values=signal_values or None,
            )
        )


def load_journal_entries() -> pd.DataFrame:
    with get_session() as session:
        entries = session.query(Journal).order_by(Journal.date.desc()).all()
        rows = []
        for entry in entries:
            trade = session.query(TradePaper).filter_by(id=entry.trade_id).first()
            rows.append(
                {
                    "id": entry.id,
                    "Datum": entry.date,
                    "Trade": f"{trade.ticker} T{trade.tranche}" if trade else f"#{entry.trade_id}",
                    "Reden": entry.reason,
                    "Verwachting": entry.expectation,
                    "Uitkomst": entry.outcome,
                }
            )
    return pd.DataFrame(rows)


def update_journal_outcome(journal_id: int, outcome: str) -> None:
    with get_session() as session:
        entry = session.query(Journal).filter_by(id=journal_id).one()
        entry.outcome = outcome


# --- Streamlit UI --------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="stock-tracker", layout="wide")
    st.title("stock-tracker")

    config = load_config()
    init_db(config)

    with get_session() as session:
        universe_size = session.query(Ticker).count()
    if universe_size == 0:
        st.warning(
            "Universum is nog leeg — draai eerst `python -m stock_tracker.ingest --full`."
        )

    tab_watchlist, tab_signals, tab_portfolio, tab_backtest, tab_journal = st.tabs(
        ["Watchlist", "Signalen vandaag", "Portfolio (paper)", "Backtest", "Journal"]
    )

    with tab_watchlist:
        st.subheader("Eligible tickers")
        st.caption(
            "**Voorlopig (5/6)**: alle criteria behalve de EPS-schatting-trend zijn "
            "gehaald — die kan pas écht beoordeeld worden zodra er ~90 dagen aan "
            "geschiedenis is opgebouwd. Geen aanbeveling, wel een startpunt om te volgen."
        )
        watchlist_default_date = latest_price_date() or date.today()
        as_of = st.date_input("Datum", value=watchlist_default_date, key="watchlist_date")
        if as_of != watchlist_default_date:
            st.caption(
                f"Laatste beschikbare koersdata: {watchlist_default_date}. Een latere datum "
                "toont geen tickers totdat de eerstvolgende dagelijkse run die dag heeft "
                "ingeladen."
            )
        df = load_watchlist(as_of, config)
        if df.empty:
            st.info("Geen eligible tickers voor deze datum (of nog geen quality-run gedaan).")
        else:
            st.dataframe(df, width="stretch", hide_index=True)

    with tab_signals:
        st.subheader("Signalen")
        st.caption(
            "Kleur geeft alleen aan hoe diep de dip is (tranche 3 = grootste koersval "
            "t.o.v. het 50-daags gemiddelde) — geen koopadvies, gewoon een leesbare "
            "weergave van de mechanische regel."
        )
        default_date = latest_signal_date() or date.today()
        as_of = st.date_input("Datum", value=default_date, key="signals_date")
        df = load_signals_for_date(as_of)
        if df.empty:
            st.info("Geen signalen voor deze datum.")
        else:
            tranche_colors = {
                3: "background-color: #ff8a65; color: black",
                2: "background-color: #ffd54f; color: black",
                1: "background-color: #dce775; color: black",
            }
            styled = df.style.apply(
                lambda row: [tranche_colors.get(row["Tranche"], "")] * len(row), axis=1
            )
            st.dataframe(styled, width="stretch", hide_index=True)

    with tab_portfolio:
        st.subheader("Open posities (paper)")
        df = load_paper_portfolio()
        if df.empty:
            st.info("Geen open posities.")
        else:
            st.dataframe(df, width="stretch", hide_index=True)

    with tab_backtest:
        st.subheader("Backtest runs")
        runs = backtest.list_runs(limit=25)
        if not runs:
            st.info("Nog geen backtest-runs opgeslagen. Draai `python -m stock_tracker.backtest`.")
        else:
            labels = [f"#{r['id']} — {r['run_at']} ({r['config_hash']})" for r in runs]
            selected = st.selectbox(
                "Run", options=range(len(runs)), format_func=lambda i: labels[i]
            )
            run_row = runs[selected]

            period = st.radio("Periode", ["out_of_sample", "in_sample"], horizontal=True)
            strategy_metric = run_row["metrics"].get(f"{period}_strategy")
            benchmark_metric = run_row["metrics"].get(f"{period}_benchmark")

            if strategy_metric is not None:
                cols = st.columns(4)
                cols[0].metric("CAGR", f"{strategy_metric.cagr_pct:.2f}%")
                cols[1].metric("Max drawdown", f"{strategy_metric.max_drawdown_pct:.2f}%")
                cols[2].metric("Sharpe", f"{strategy_metric.sharpe:.2f}")
                trades_label = (
                    strategy_metric.num_trades if strategy_metric.num_trades is not None else "n/a"
                )
                cols[3].metric("Trades", trades_label)

                strategy_curve = backtest.load_equity_curve(strategy_metric)
                benchmark_curve = (
                    backtest.load_equity_curve(benchmark_metric) if benchmark_metric else []
                )

                if strategy_curve:
                    chart_df = pd.DataFrame(
                        {d: v for d, v in strategy_curve}.items(), columns=["Datum", "Strategie"]
                    ).set_index("Datum")
                    if benchmark_curve:
                        bench_df = pd.DataFrame(
                            {d: v for d, v in benchmark_curve}.items(),
                            columns=["Datum", "Benchmark"],
                        ).set_index("Datum")
                        chart_df = chart_df.join(bench_df, how="outer")
                    st.line_chart(chart_df)

                    equity_series = pd.Series(
                        [v for _, v in strategy_curve],
                        index=pd.DatetimeIndex([d for d, _ in strategy_curve]),
                    )
                    drawdown_pct = (equity_series / equity_series.cummax() - 1) * 100
                    st.area_chart(drawdown_pct.rename("Drawdown %"))

            if run_row["notes"]:
                st.warning(run_row["notes"])

    with tab_journal:
        st.subheader("Nieuwe entry")
        trades_df = load_recent_trades()
        if trades_df.empty:
            st.info("Nog geen paper trades om te journalen.")
        else:
            labels_map = dict(zip(trades_df["id"], trades_df["label"], strict=True))
            selected_id = st.selectbox(
                "Trade", options=trades_df["id"].tolist(), format_func=lambda i: labels_map[i]
            )
            reason = st.text_area("Reden voor deze trade")
            expectation = st.text_area("Verwachting")
            if st.button("Opslaan"):
                save_journal_entry(selected_id, reason, expectation)
                st.success("Journal-entry opgeslagen.")
                st.rerun()

        st.subheader("Bestaande entries")
        journal_df = load_journal_entries()
        if journal_df.empty:
            st.info("Nog geen journal-entries.")
        else:
            st.dataframe(journal_df.drop(columns=["id"]), width="stretch", hide_index=True)
            st.subheader("Uitkomst toevoegen")
            entry_id = st.selectbox("Entry", options=journal_df["id"].tolist())
            outcome = st.text_area("Uitkomst")
            if st.button("Uitkomst opslaan"):
                update_journal_outcome(entry_id, outcome)
                st.success("Uitkomst opgeslagen.")
                st.rerun()


if __name__ == "__main__":
    main()
