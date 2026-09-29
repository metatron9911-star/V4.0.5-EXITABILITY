# BBB — BybitBot Beta

Status: **PAPER / SHADOW ONLY**. No exchange order submission is enabled in this scaffold.

BBB is the replacement research slot for the old S1/V7 line. The old V7 code remains intact on `main`; BBB is isolated on branch `bbb-bybitbot-v0` until the new contour is validated.

## Goal

Build a fast perpetual-futures bot using the proven RHB engineering pattern while keeping venue and product-class strategy logic separate.

Core invariants:
- two-wallet accounting: **TRADING WALLET** + **PROFIT WALLET**;
- explicit signal and machine-state indicators;
- full trade ledger, including fees, funding, spread, modeled slippage, latency and MAE/MFE;
- paper fills must use observable bid/ask/orderbook, not chart last-price;
- every head has its own performance attribution but shares one global capital ledger;
- no LIVE path until paper protocol is closed separately.

## Head layout v0

1. **HEAD-LIQUID** — standard high-liquidity perps: SOL, ETH, BTC.
2. **HEAD-VOL** — volatile mid/small-cap standard perps.
3. **HEAD-LAUNCH** — Bybit Pre-Market Perpetual / Innovation Zone.

Do **not** put launch/pre-market into the same strategy as standard perps. Its fee schedule, liquidity, funding and price-limit behavior are materially different.

Hyperliquid and Binance are venue adapters, not new strategy heads by themselves. A strategy head is selected by market microstructure/product class; the venue adapter handles feed/execution semantics.

## RHB ideas intentionally reused

- Start bank / cash / trading equity / sizing bank / cash reserve.
- Separate PAPER PROFIT WALLET removed from active trading capital.
- OPEN / PENDING / CLOSED, W/L, WR, realized PnL, fees, max drawdown.
- signal score + gate + risk state + circuit-breaker state.
- feed age, processing lag, signal-to-fill latency.
- immutable closed-trade record with entry attribution.
- persistent session/experiment identity; restart must not fabricate a new experiment.

See:
- `docs/MARKET_RESEARCH_2026-09-29.md`
- `docs/ARCHITECTURE_V0.md`
- `config.example.json`
- `schema.sql`
- `engine.py`

