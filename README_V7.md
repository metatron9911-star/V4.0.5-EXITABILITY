# V7.1 — diversified 30-position PAPER build

V7.1 keeps real execution disabled. It is a PAPER experiment only.

## Core rules
- Starting PAPER bank: $150.
- Up to 30 simultaneous mints. Capital is divided across the remaining free slots; from an empty account the nominal first allocation is about $5 per mint, not $50.
- Scanner target interval: 10 seconds. Exit checks: 3 seconds.
- At executable +100% growth, the engine attempts to sell half only when that sale actually recovers the original position body plus modeled fee. The remainder keeps running.
- Full exit trigger: executable value falls 2% below the entry value of the tokens still held. This is a trigger, not a guaranteed fill price.
- No time exit and no trailing exit in this build.
- Hourly Telegram status report when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are configured. Manual Telegram buttons remain available.
- Security, route, liquidity and round-trip checks remain fail-closed. The engine does not buy merely to keep itself busy.

## Replace repository files
Upload all files from this package to the repository root with the exact names. Railway Start Command remains:

    python run.py

Use one Railway replica and persistent DATA_DIR=/data. V7.1 uses `v7_1.sqlite3`, so it does not collide with the previous V7 database/config hash.

Required for market checks: JUPITER_API_KEY plus HELIUS_API_KEY or SOLANA_RPC_URL. Telegram hourly reports require TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

Run locally:

    python -m pip install -r requirements.txt
    python -m unittest test_v7 -v
    python run.py
