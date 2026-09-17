# V7.3 CORE PAPER

PAPER-only build. There is no wallet signing or transaction submission path.

## Why this rebuild exists
The V7.2 one-hour protocol showed confirmed candidates repeatedly dying at `TOKEN_PROGRAM_UNSUPPORTED`, plus large volumes of `INVALID_PAIR` / `PAIR_UNAVAILABLE`. V7.3 fixes the plumbing before attempting faster streaming.

## Changes
- Supports both the classic SPL Token program and Token-2022 in the security gate. Unknown token programs still fail closed.
- Security results are cached for 300 s, so the same rejected mint is not RPC/RugCheck-tested every 5 s.
- Missing 5-minute DEX statistics become normal filter failures rather than `INVALID_PAIR` parser failures.
- DEX pair selection prefers Solana base-token pairs with usable USD price/liquidity.
- One temporarily unpriced open position no longer freezes all new entries; sizing remains cash-based and equity remains UNKNOWN until marks recover.
- Up to 60 candidates per scan; scanner 5 s; open-position exit checks target 1 s.
- TP and SL are based on the current executable Jupiter minimum-output quote: +1.5% net PAPER TP / -2% net PAPER stop under the model.
- New isolated DB: `v7_3.sqlite3`.
- Hourly Telegram status retained.
- Excel download retained; filename starts `V7_3_protocol_`.

## Current market assumptions
Pump bonding-curve trades currently charge 1.25% per trade; PumpSwap canonical-pool fees vary with market cap. Jupiter executable quote/minOut is therefore used instead of chart-price-only exits. Network/priority cost is still a configurable PAPER estimate and real execution can differ.

## Deploy
Replace the repo contents with this package. Railway start command remains `python run.py`; keep one replica and `DATA_DIR=/data`. Keep API keys in Railway variables, not GitHub.

## Verify
Run `python -m unittest test_v7 -v`. Dashboard should show V7.3 and a fresh `v7-3-core-paper-001` experiment.
