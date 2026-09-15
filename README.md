# V4 Momentum Hunter v4.0.2 — PAPER + STATUS

## What changed
- Source/module status indicators on the dashboard: Birdeye, DEX Screener, Helius, Telegram, SQLite, AUTO scanner, PAPER engine, hard lock.
- Background PAPER scanning; no need to press Scan manually.
- DEX Screener public discovery is the fast discovery loop.
- Birdeye new listings are budget-throttled separately (`BIRDEYE_POLL_INTERVAL_SEC`, default 1800 sec) so the free Standard package is not burned by health checks.
- Last-scan diagnostics: discovered → pairs found → filters → eligible → PAPER opened.
- Hourly Telegram health/status heartbeat.
- Telegram `/status` and `/health` commands return current source/module health and PAPER statistics.
- `Send status to Telegram`, `Refresh status`, and `Test Telegram` dashboard buttons.
- Open PAPER positions are repriced/managed every scan even if the token disappears from discovery feeds.
- AUTO execution remains physically absent/disabled.

## Railway variables
Required:
- `BIRDEYE_API_KEY`
- `HELIUS_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `DATA_DIR=/data`
- `V4_MODE=PAPER`
- `AUTO_TRADING_ENABLED=false`

Recommended defaults:
- `AUTO_SCAN_INTERVAL_SEC=30`
- `BIRDEYE_POLL_INTERVAL_SEC=1800`
- `STATUS_HEARTBEAT_MINUTES=60`

Do **not** set `SOLANA_PRIVATE_KEY_B58` in this PAPER build.

## Update from v4.0.1
Upload/replace these files in GitHub **with exactly these names**:
- `app.py`
- `engine.py`
- `telegram_notify.py`
- `monitoring.py` (new)
- `config.json`

`Procfile` and `requirements.txt` can remain as they are, but the full ZIP includes them.

## Telegram
Send `/status` or `/health` to the V4 bot. The bot accepts status commands only from the configured `TELEGRAM_CHAT_ID`.

## Safety
This build contains no real trade execution path. `execution_DISABLED.py` remains in the package and `AUTO_TRADING_ENABLED=false` should stay set.
