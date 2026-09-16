# V4 Momentum Hunter v4.0.8 — PAPER recovery build

This release fixes the zero-entry experiment without enabling real trading.

## Why v4.0.7 recorded no PAPER buys

The scanner and snapshot pipeline worked, but the entry path required a successful
Jupiter token-to-USDC quote. `Jupiter HTTP 503`, timeouts, a missing key, or missing
Helius decimals therefore blocked every candidate before `open_paper()`.

## What changed

- Every evaluated candidate is written to `entry_decisions` with an exact reason.
- The Excel protocol includes an `ENTRY_DECISIONS` sheet.
- Entry gates are calibrated to the supplied protocol: liquidity >= $10,000,
  5-minute volume >= $100, score >= 40, risk <= 35, buys > sells.
- Jupiter remains the preferred executable sell-route check.
- In `PAPER` mode only, a transient Jupiter failure may use a strict DEX proxy when
  liquidity >= $20,000 and 5-minute volume >= $1,000.
- The DEX proxy never signs or submits an order and cannot enable AUTO trading.
- Initial PAPER stop is 10%; after the first harvest, the working leg trails 20%
  below its observed peak.
- Profit cycles harvest 10% of the working base every +15%. After three harvests,
  20% of remaining tokens become a moonbag with x2/x5/x10 partial exits.
- Dashboard and Telegram status expose low volume, weak flow, route failures,
  fallback entries, and position-opening blocks.

## GitHub replacement

Replace the repository files with the files from this package, keeping their exact
names. Railway should redeploy automatically.

Required Railway variables remain:

- `DATA_DIR=/data`
- `V4_MODE=PAPER`
- `AUTO_TRADING_ENABLED=false`
- `BIRDEYE_API_KEY`
- `HELIUS_API_KEY`
- `JUPITER_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Do not add a Solana private key. Real execution remains physically absent through
`execution_DISABLED.py`.

## First verification after deploy

1. Confirm the dashboard title shows `v4.0.8`.
2. Confirm Sources & modules are healthy; a temporary Jupiter error is acceptable
   for PAPER but must be visible.
3. Wait for one scan and open `Entry decisions`.
4. Confirm every candidate has `REJECT`, `BLOCK`, or `OPEN` plus a reason.
5. Download the 1-hour protocol and confirm `ENTRY_DECISIONS` is populated.
6. Keep PAPER running. Do not enable real execution.

