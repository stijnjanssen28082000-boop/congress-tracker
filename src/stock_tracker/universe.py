"""Builds and persists the trading universe: S&P 500 + Euronext 100 + AEX + BEL 20.

Constituent lists are scraped from Wikipedia (no free, reliable API covers all
four indices with sector data). Each `fetch_*` function is a thin, mockable
boundary around the network/parsing so `build_universe` and
`sync_universe_to_db` can be tested without hitting the network.
"""

from __future__ import annotations

import io
import logging

import pandas as pd
import requests

from stock_tracker.db.models import Ticker
from stock_tracker.db.session import get_session

logger = logging.getLogger("stock_tracker.universe")

WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_EURONEXT100_URL = "https://en.wikipedia.org/wiki/Euronext_100"
WIKI_AEX_URL = "https://en.wikipedia.org/wiki/AEX_index"
WIKI_BEL20_URL = "https://en.wikipedia.org/wiki/BEL_20"

# Wikipedia blocks plain urllib/pandas requests (no User-Agent) with a 403;
# fetch the HTML ourselves via requests (which identifies as a browser) and
# hand pandas the text instead of letting it open the URL itself.
_USER_AGENT = (
    "Mozilla/5.0 (compatible; stock-tracker/1.0; "
    "+https://github.com/stijnjanssen28082000-boop/congress-tracker)"
)


def _read_first_matching_table(
    url: str, ticker_col: str, name_col_candidates: list[str]
) -> tuple[pd.DataFrame, str]:
    """Returns the first table containing `ticker_col` plus one of
    `name_col_candidates`, and which of those name columns matched.

    Wikipedia's index-constituent pages aren't all built from the same
    template — the company-name column is "Company" on some pages and "Name"
    on others — so multiple candidates are tried instead of a single fixed
    column.
    """

    response = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=30)
    response.raise_for_status()
    tables = pd.read_html(io.StringIO(response.text))
    for table in tables:
        if ticker_col not in table.columns:
            continue
        for name_col in name_col_candidates:
            if name_col in table.columns:
                return table, name_col
    raise ValueError(
        f"No table with column {ticker_col!r} and one of {name_col_candidates} found at {url}"
    )


def fetch_sp500() -> list[dict]:
    table, name_col = _read_first_matching_table(WIKI_SP500_URL, "Symbol", ["Security"])
    entries = []
    for _, row in table.iterrows():
        entries.append(
            {
                "ticker": str(row["Symbol"]).replace(".", "-"),
                "name": str(row[name_col]),
                "exchange": "US",
                "currency": "USD",
                "sector": row.get("GICS Sector"),
                "index_membership": "SP500",
            }
        )
    return entries


def fetch_euronext100() -> list[dict]:
    table, name_col = _read_first_matching_table(
        WIKI_EURONEXT100_URL, "Ticker", ["Company", "Name"]
    )
    entries = []
    for _, row in table.iterrows():
        entries.append(
            {
                "ticker": str(row["Ticker"]),
                "name": str(row[name_col]),
                "exchange": "Euronext",
                "currency": "EUR",
                "sector": row.get("ICB Sector") or row.get("Sector"),
                "index_membership": "EURONEXT100",
            }
        )
    return entries


def fetch_aex() -> list[dict]:
    table, name_col = _read_first_matching_table(WIKI_AEX_URL, "Ticker", ["Company", "Name"])
    entries = []
    for _, row in table.iterrows():
        entries.append(
            {
                "ticker": str(row["Ticker"]),
                "name": str(row[name_col]),
                "exchange": "Euronext Amsterdam",
                "currency": "EUR",
                "sector": row.get("Sector") or row.get("ICB Sector"),
                "index_membership": "AEX",
            }
        )
    return entries


def fetch_bel20() -> list[dict]:
    table, name_col = _read_first_matching_table(WIKI_BEL20_URL, "Ticker", ["Company", "Name"])
    entries = []
    for _, row in table.iterrows():
        entries.append(
            {
                "ticker": str(row["Ticker"]),
                "name": str(row[name_col]),
                "exchange": "Euronext Brussels",
                "currency": "EUR",
                "sector": row.get("Sector") or row.get("ICB Sector"),
                "index_membership": "BEL20",
            }
        )
    return entries


BENCHMARK_TICKER = "^GSPC"


def fetch_benchmark() -> list[dict]:
    """Adds the S&P 500 index itself as a ticker, so it gets ingested (prices
    only — it has no fundamentals) alongside the rest of the universe. Module
    4's backtester uses it as the buy & hold benchmark. It never appears in
    `quality_scores`/eligible lists since it has no fundamentals to score."""

    return [
        {
            "ticker": BENCHMARK_TICKER,
            "name": "S&P 500 Index",
            "exchange": "INDEX",
            "currency": "USD",
            "sector": None,
            "index_membership": "BENCHMARK",
        }
    ]


DEFAULT_FETCHERS = (fetch_sp500, fetch_euronext100, fetch_aex, fetch_bel20, fetch_benchmark)


def build_universe(fetchers=DEFAULT_FETCHERS) -> list[dict]:
    """Merges all index constituents, deduplicating by ticker and combining
    `index_membership` (comma-separated) for tickers listed in multiple indices."""

    merged: dict[str, dict] = {}
    for fetch in fetchers:
        try:
            entries = fetch()
        except Exception:
            logger.exception("Failed to fetch universe constituents from %s", fetch.__name__)
            continue

        for entry in entries:
            ticker = entry["ticker"]
            if ticker in merged:
                existing_memberships = merged[ticker]["index_membership"].split(",")
                if entry["index_membership"] not in existing_memberships:
                    merged[ticker]["index_membership"] = ",".join(
                        [*existing_memberships, entry["index_membership"]]
                    )
            else:
                merged[ticker] = dict(entry)

    return list(merged.values())


def sync_universe_to_db(entries: list[dict]) -> int:
    """Upserts universe entries into the `tickers` table. Returns the number
    of tickers written. Tickers no longer in `entries` are left untouched
    (not deactivated) — that's Module 2's job (`eligible` list), not Module 1's."""

    with get_session() as session:
        existing = {t.ticker: t for t in session.query(Ticker).all()}
        for entry in entries:
            ticker_row = existing.get(entry["ticker"])
            if ticker_row is None:
                session.add(Ticker(**entry))
            else:
                ticker_row.name = entry["name"]
                ticker_row.exchange = entry["exchange"]
                ticker_row.currency = entry["currency"]
                ticker_row.sector = entry["sector"]
                ticker_row.index_membership = entry["index_membership"]
    return len(entries)
