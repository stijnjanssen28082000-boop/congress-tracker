"""Daily orchestrator: ingest --daily -> quality (Mondays only, or whichever
weekday `quality.recompute_weekday` names) -> signals -> paper-portfolio
update -> Telegram alert. Meant to run once per trading day (GitHub Actions
or local cron — see README)."""

from __future__ import annotations

import argparse
import calendar
import logging
from datetime import date

from dotenv import load_dotenv

from stock_tracker import ingest, paper, quality, signals
from stock_tracker.alerts import format_daily_alert, format_milestone_alert, send_telegram_message
from stock_tracker.config import Config, load_config
from stock_tracker.db.models import TradePaper
from stock_tracker.db.session import get_session, init_db
from stock_tracker.logging_setup import setup_logging

logger = logging.getLogger("stock_tracker.run_daily")


def _is_quality_day(as_of: date, config: Config) -> bool:
    target_weekday = list(calendar.day_name).index(config.quality.recompute_weekday)
    return as_of.weekday() == target_weekday


def _count_closed_trades() -> int:
    with get_session() as session:
        return session.query(TradePaper).filter(TradePaper.status == "CLOSED").count()


def run(as_of: date | None = None, config: Config | None = None) -> dict:
    config = config or load_config()
    as_of = as_of or date.today()

    logger.info("=== Daily run for %s ===", as_of)

    ingest.run_daily(config)

    if _is_quality_day(as_of, config):
        logger.info("%s is the configured quality-recompute day", config.quality.recompute_weekday)
        quality.run_for_date(as_of, config)

    entries = signals.generate_entry_signals(as_of, config)
    exits = signals.generate_exit_signals(as_of, config)
    signals.store_signals(entries + exits)

    closed_before = _count_closed_trades()
    fill_result = paper.fill_pending_signals(as_of, config)
    closed_after = _count_closed_trades()

    alert_text = format_daily_alert(as_of, entries, exits)
    alert_sent = bool(alert_text) and send_telegram_message(alert_text)

    milestone_sent = False
    threshold = config.paper_trading.review_after_closed_trades
    if closed_before < threshold <= closed_after:
        milestone = paper.compare_to_backtest(config)
        if milestone:
            milestone_sent = send_telegram_message(format_milestone_alert(milestone))

    result = {
        "as_of": as_of,
        "entries": len(entries),
        "exits": len(exits),
        "paper_opened": fill_result["opened"],
        "paper_closed": fill_result["closed"],
        "alert_sent": alert_sent,
        "milestone_sent": milestone_sent,
    }
    logger.info("Daily run complete: %s", result)
    return result


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="stock-tracker daily run")
    parser.add_argument(
        "--date", type=date.fromisoformat, default=None, help="Run as of this date (default: today)"
    )
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(config)
    init_db(config)

    run(args.date, config)


if __name__ == "__main__":
    main()
