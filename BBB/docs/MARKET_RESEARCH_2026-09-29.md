# BBB market research — 2026-09-29

This note records the market-data, fee and execution assumptions used for the first BBB paper build. All percentages are of **notional**, not margin.

## Priority table

| Priority | Product class | Venue | Base maker | Base taker | Taker→taker round trip | Feed / documented cadence | BBB treatment |
|---|---|---|---:|---:|---:|---|---|
| P1 | Standard volatile USDT perpetuals | Bybit | 0.020% | 0.055% | 0.110% | publicTrade real-time; L1 10 ms; L50 20 ms | HEAD-VOL |
| P1 | SOL / ETH / BTC standard perpetuals | Bybit | 0.020% | 0.055% | 0.110% | same feed | HEAD-LIQUID |
| P2 | Pre-Market Perpetual | Bybit | 0.040% | 0.100% | 0.200% | public trade feed only after ContinuousTrading; funding fixed 0.005% / 4 h in continuous auction | HEAD-LAUNCH |
| P2 | Innovation Zone perpetual | Bybit | 0.040% | 0.110% | 0.220% | same Bybit linear WS family | HEAD-LAUNCH |
| P3 | USDⓈ-M standard perpetuals | Binance | 0.020% | 0.050% | 0.100% | aggTrade real-time; depth supports 100 ms mode | alternate venue adapter |
| P3 | Standard perps | Hyperliquid | 0.015% | 0.045% | 0.090% | trades WS; L2 snapshot is block-driven, at least ~0.5 s between book pushes | alternate venue adapter |

### Official-source notes

**Bybit**
- VIP0 standard perpetual/futures: maker 0.020%, taker 0.055%.
- Pre-Market Perpetual VIP0: maker 0.040%, taker 0.100%.
- Innovation Zone VIP0: maker 0.040%, taker 0.110%.
- Public trade topic is pushed real-time.
- Linear/inverse orderbook push: L1 10 ms, L50 20 ms, L200 100 ms, L1000 200 ms.
- PreLaunch trade feed does not exist until ContinuousTrading.
- Fast private execution stream is available and explicitly documented as lower-latency than the normal execution stream.
- Funding interval/rate is symbol-specific. Pre-market continuous auction funding is fixed at 0.005% every 4 hours; call auction funding is zero.

**Binance USDⓈ-M**
- Regular user USDT-M: maker 0.020%, taker 0.050%.
- With the documented BNB 10% fee discount: 0.018% / 0.045%.
- Futures funding is separate from commission. Default funding interval is 8h but can change by contract and market conditions.
- Use WebSocket user data for order/position state during volatility rather than REST.

**Hyperliquid**
- Perps tier-0 base fee: maker 0.015%, taker 0.045%.
- HIP-3 may add deployer-fee scaling; growth mode can reduce all-in fees by >=90%, so each HIP-3 market must be queried rather than assuming the base rate.
- Funding is peer-to-peer and settled hourly.
- Co-located order request -> committed response is documented around median 0.2 s / p99 0.9 s.
- Standard API L2 snapshots are block-driven; a local non-validating node is the low-latency path.

## What actually costs a round trip

For BBB the true cost is:

```
all_in_cost =
  entry_commission
+ exit_commission
+ entry_half_spread
+ exit_half_spread
+ entry_market_impact
+ exit_market_impact
+ latency_adverse_move
+ funding_if_crossed
```

There is no per-trade withdrawal fee. Deposit/withdrawal/network fees belong to treasury operations, not trade PnL.

Leverage does **not** reduce commission: the fee is charged on position notional. Therefore the ledger stores both margin and notional.

## Cost floors used in PAPER

BBB must not hard-code a fake slippage percentage as the only cost model. At signal time it records:
- best bid / best ask;
- spread bps;
- L2 quantity available through target notional;
- expected VWAP for immediate entry;
- fee tier;
- funding rate + next funding timestamp.

At paper fill it records the later book snapshot and calculates:
- signal mid -> executable VWAP drift;
- modeled taker fee;
- realized/model funding;
- total cost bps.

The strategy must beat **expected round-trip all-in cost + safety margin**, not merely beat zero.

## Latency measurements required

Exchange docs give publish cadence, not our real end-to-end latency. BBB therefore measures four clocks:

1. `source_age_ms = local_receive - exchange_event_ts`
2. `processing_ms = signal_ready - local_receive`
3. `decision_to_order_ms = order_send - signal_ready`
4. `signal_to_fill_ms = fill_ts - signal_ready`

For PAPER, #3/#4 become simulated using observable future book events; for LIVE later they use private execution updates.

## Strategy consequence

Standard liquid perps and standard volatile alts can share the same feature engine, but **not the same thresholds/risk/exit parameters**.

Pre-market/innovation must be a separate head because:
- taker round-trip commission is ~0.20–0.22% before spread/slippage;
- liquidity and spread distribution are different;
- fixed pre-market funding behavior is different;
- price-limit and transition mechanics differ;
- launch-phase volatility makes ordinary stop/timeout assumptions unsafe to transplant unchanged.

## Source URLs

- Bybit fees: https://www.bybit.com/en/help-center/article/Trading-Fee-Structure
- Bybit public trades: https://bybit-exchange.github.io/docs/v5/websocket/public/trade
- Bybit orderbook: https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
- Bybit fast execution: https://bybit-exchange.github.io/docs/v5/websocket/private/fast-execution
- Bybit pre-market FAQ: https://www.bybit.com/en/help-center/article/FAQ-Pre-Market-Perpetual
- Binance futures fees: https://www.binance.com/en/fee/futureFee
- Binance funding: https://www.binance.com/en/support/faq/detail/360033525031
- Hyperliquid fees: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
- Hyperliquid WS: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket
- Hyperliquid latency: https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/optimizing-latency
