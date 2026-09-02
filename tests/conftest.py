import pytest

from stock_tracker.config import Config
from stock_tracker.db.session import init_db, reset_engine_cache


@pytest.fixture
def db(tmp_path):
    """Isolated SQLite DB for a single test, wired into the module-level
    engine cache so code under test can call get_session()/init_db() with no
    arguments and still hit the temp DB."""
    reset_engine_cache()
    config = Config(
        {
            "database": {"path": str(tmp_path / "test.db")},
            "logging": {"dir": str(tmp_path / "logs"), "level": "INFO"},
            "ingest": {
                "full_history_years": 12,
                "price_provider": "yfinance",
                "fundamentals_provider": "fmp",
            },
        }
    )
    init_db(config)
    yield config
    reset_engine_cache()
