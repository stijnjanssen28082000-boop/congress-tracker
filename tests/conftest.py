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
                "fundamentals_provider": "yfinance",
            },
            "quality": {
                "min_market_cap_eur": 30_000_000_000,
                "min_revenue_growth_ttm_pct": 8.0,
                "max_net_debt_to_ebitda": 2.0,
                "min_avg_daily_volume": 500_000,
                "avg_volume_lookback_days": 60,
                "eps_estimate_lookback_days": 90,
                "recompute_weekday": "Monday",
            },
            "signals": {
                "tranche_1": {"sma50_discount_pct": 8.0, "rsi_threshold": 35, "rsi_period": 14},
                "tranche_2": {"sma50_discount_pct": 15.0},
                "tranche_3": {"sma50_discount_pct": 25.0},
                "sma_period": 50,
                "high_lookback_weeks": 52,
                "exit": {
                    "profit_target_pct": 10.0,
                    "time_stop_weeks": 12,
                    "eps_estimate_drop_pct": 10.0,
                },
                "earnings_guard_days": 5,
            },
            "backtest": {
                "initial_capital_eur": 100_000,
                "in_sample_start": "2012-01-01",
                "in_sample_end": "2019-12-31",
                "out_of_sample_start": "2020-01-01",
                "out_of_sample_end": None,
                "risk_free_rate_pct": 0.0,
                "position_sizing": {
                    "max_pct_per_tranche": 2.0,
                    "max_pct_per_ticker": 6.0,
                    "max_pct_per_sector": 25.0,
                    "min_cash_pct": 20.0,
                },
                "costs": {
                    "slippage_pct": 0.1,
                    "belgian_tob_pct": 0.35,
                    "capital_gains_tax_pct": 10.0,
                    "broker_fee_pct": 0.0,
                },
                "benchmark": "SP500",
                "overfitting_check": {
                    "cagr_degradation_pct": 50.0,
                    "sharpe_floor": 0.0,
                    "in_sample_sharpe_threshold": 0.5,
                },
            },
            "alerts": {
                "telegram_enabled": True,
                "daily_run_time_cet": "07:00",
            },
            "paper_trading": {
                "starting_capital_eur": 100_000,
                "monthly_report": True,
                "review_after_closed_trades": 30,
                "win_rate_tolerance_pct": 10.0,
                "avg_profit_tolerance_pct": 5.0,
            },
        }
    )
    init_db(config)
    yield config
    reset_engine_cache()
