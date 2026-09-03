"""Telegram alerts: formats the day's signals/milestones into a message and
sends it via the Telegram Bot API. No message is sent when there's nothing
to report."""

from __future__ import annotations

import logging
import os
from datetime import date

import requests

from stock_tracker.signals import EXIT, EXIT_FUNDAMENTAL, REVIEW, SignalResult

logger = logging.getLogger("stock_tracker.alerts")

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def format_daily_alert(
    as_of: date, entries: list[SignalResult], exits: list[SignalResult]
) -> str | None:
    """Builds the daily alert text, or None if there's nothing to report."""

    reviews = [s for s in exits if s.signal_type == REVIEW]
    real_exits = [s for s in exits if s.signal_type in (EXIT, EXIT_FUNDAMENTAL)]

    if not entries and not real_exits and not reviews:
        return None

    lines = [f"stock-tracker — {as_of.isoformat()}"]

    if entries:
        lines.append("")
        lines.append(f"Nieuwe entries ({len(entries)}):")
        for sig in sorted(entries, key=lambda s: (-(s.tranche or 0), s.ticker)):
            rsi_part = f" (RSI {sig.rsi14:.0f})" if sig.rsi14 is not None else ""
            lines.append(f"  T{sig.tranche} {sig.ticker}  {sig.price:.2f}{rsi_part}")

    if real_exits:
        lines.append("")
        lines.append(f"Exits ({len(real_exits)}):")
        for sig in real_exits:
            tranche_label = f"T{sig.tranche}" if sig.tranche else "alle tranches"
            lines.append(f"  {sig.signal_type} {sig.ticker} {tranche_label} — {sig.notes or ''}")

    if reviews:
        lines.append("")
        lines.append(f"REVIEW-flags ({len(reviews)}):")
        for sig in reviews:
            lines.append(f"  {sig.ticker} T{sig.tranche} — {sig.notes or ''}")

    return "\n".join(lines)


def format_milestone_alert(milestone: dict) -> str:
    lines = [f"Milestone: {milestone['num_closed_trades']} paper trades gesloten"]
    lines.append(
        f"Win rate: {milestone['win_rate_pct']:.1f}% "
        f"(backtest: {milestone['backtest_win_rate_pct']})"
    )
    if milestone["avg_profit_pct"] is not None:
        lines.append(
            f"Gem. winst/trade: {milestone['avg_profit_pct']:.2f}% "
            f"(backtest: {milestone['backtest_avg_profit_pct']})"
        )
    return "\n".join(lines)


def send_telegram_message(text: str) -> bool:
    """Sends `text` via the Telegram Bot API. Returns True if sent; False if
    Telegram isn't configured (logs the message instead) or the request fails."""

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning(
            "Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) — message:\n%s", text
        )
        return False

    url = TELEGRAM_API_URL.format(token=token)
    try:
        response = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=30)
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Failed to send Telegram alert")
        return False
    return True
