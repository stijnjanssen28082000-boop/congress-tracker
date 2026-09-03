import pytest

from stock_tracker.db.models import Ticker
from stock_tracker.db.session import get_session
from stock_tracker.universe import (
    BENCHMARK_TICKER,
    DEFAULT_FETCHERS,
    _read_first_matching_table,
    build_universe,
    fetch_aex,
    fetch_bel20,
    fetch_benchmark,
    fetch_euronext100,
    sync_universe_to_db,
)


def _mock_html_response(mocker, html: str):
    mock_response = mocker.MagicMock()
    mock_response.text = html
    mock_response.raise_for_status = mocker.MagicMock()
    mocker.patch("stock_tracker.universe.requests.get", return_value=mock_response)


_COMPANY_COLUMN_TABLE = """
<table>
<tr><th>Ticker</th><th>Company</th><th>Sector</th></tr>
<tr><td>ASML</td><td>ASML Holding</td><td>Technology</td></tr>
</table>
"""

_NAME_COLUMN_TABLE = """
<table>
<tr><th>Ticker</th><th>Name</th><th>ICB Sector</th></tr>
<tr><td>UCB</td><td>UCB</td><td>Health Technology</td></tr>
</table>
"""


def _fake_sp500():
    return [
        {
            "ticker": "AAPL",
            "name": "Apple",
            "exchange": "US",
            "currency": "USD",
            "sector": "Technology",
            "index_membership": "SP500",
        }
    ]


def _fake_euronext100():
    return [
        {
            "ticker": "ASML",
            "name": "ASML",
            "exchange": "Euronext",
            "currency": "EUR",
            "sector": "Technology",
            "index_membership": "EURONEXT100",
        }
    ]


def _fake_aex():
    # ASML is a member of both Euronext 100 and AEX.
    return [
        {
            "ticker": "ASML",
            "name": "ASML Holding",
            "exchange": "Euronext Amsterdam",
            "currency": "EUR",
            "sector": "Technology",
            "index_membership": "AEX",
        }
    ]


def _failing_fetcher():
    raise RuntimeError("network unavailable")


def test_build_universe_merges_membership_for_overlapping_ticker():
    universe = build_universe(fetchers=(_fake_sp500, _fake_euronext100, _fake_aex))
    by_ticker = {e["ticker"]: e for e in universe}

    assert by_ticker["AAPL"]["index_membership"] == "SP500"
    assert by_ticker["ASML"]["index_membership"] == "EURONEXT100,AEX"
    assert len(universe) == 2


def test_build_universe_skips_failing_fetcher():
    universe = build_universe(fetchers=(_fake_sp500, _failing_fetcher))
    assert len(universe) == 1


def test_fetch_benchmark_returns_sp500_index():
    entries = fetch_benchmark()
    assert len(entries) == 1
    assert entries[0]["ticker"] == BENCHMARK_TICKER
    assert entries[0]["index_membership"] == "BENCHMARK"
    assert entries[0]["sector"] is None


def test_default_fetchers_include_benchmark():
    assert fetch_benchmark in DEFAULT_FETCHERS


def test_read_first_matching_table_picks_first_matching_name_column(mocker):
    _mock_html_response(mocker, _NAME_COLUMN_TABLE)
    table, name_col = _read_first_matching_table(
        "https://example.com", "Ticker", ["Company", "Name"]
    )
    assert name_col == "Name"
    assert table.iloc[0]["Ticker"] == "UCB"


def test_read_first_matching_table_raises_when_nothing_matches(mocker):
    _mock_html_response(mocker, _COMPANY_COLUMN_TABLE)
    with pytest.raises(ValueError):
        _read_first_matching_table("https://example.com", "Ticker", ["Nonexistent"])


def test_fetch_euronext100_supports_company_column(mocker):
    _mock_html_response(mocker, _COMPANY_COLUMN_TABLE)
    entries = fetch_euronext100()
    assert entries == [
        {
            "ticker": "ASML",
            "name": "ASML Holding",
            "exchange": "Euronext",
            "currency": "EUR",
            "sector": "Technology",
            "index_membership": "EURONEXT100",
        }
    ]


def test_fetch_euronext100_supports_name_column(mocker):
    _mock_html_response(mocker, _NAME_COLUMN_TABLE)
    entries = fetch_euronext100()
    assert entries[0]["ticker"] == "UCB"
    assert entries[0]["name"] == "UCB"
    assert entries[0]["sector"] == "Health Technology"


def test_fetch_bel20_supports_name_column(mocker):
    _mock_html_response(mocker, _NAME_COLUMN_TABLE)
    entries = fetch_bel20()
    assert entries[0]["ticker"] == "UCB"
    assert entries[0]["index_membership"] == "BEL20"


def test_fetch_aex_supports_company_column(mocker):
    _mock_html_response(mocker, _COMPANY_COLUMN_TABLE)
    entries = fetch_aex()
    assert entries[0]["ticker"] == "ASML"
    assert entries[0]["index_membership"] == "AEX"


def test_sync_universe_to_db_inserts_and_updates(db):
    entries = [
        {
            "ticker": "AAPL",
            "name": "Apple",
            "exchange": "US",
            "currency": "USD",
            "sector": "Technology",
            "index_membership": "SP500",
        }
    ]

    count = sync_universe_to_db(entries)
    assert count == 1
    with get_session() as session:
        row = session.query(Ticker).filter_by(ticker="AAPL").one()
        assert row.name == "Apple"

    entries[0]["name"] = "Apple Inc."
    sync_universe_to_db(entries)
    with get_session() as session:
        row = session.query(Ticker).filter_by(ticker="AAPL").one()
        assert row.name == "Apple Inc."
        assert session.query(Ticker).count() == 1
