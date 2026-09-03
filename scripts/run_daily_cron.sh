#!/usr/bin/env bash
# Local cron entrypoint for the daily run. Unlike the GitHub Actions workflow,
# there's nothing to restore/save — data/tracker.db already persists on disk
# between runs.
#
# Example crontab entry (07:00 every weekday, adjust for your local timezone):
#   0 7 * * 1-5 /path/to/stock-tracker/scripts/run_daily_cron.sh >> /path/to/stock-tracker/logs/cron.log 2>&1

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
uv run python -m stock_tracker.run_daily
