# BBB architecture v0

## 1. Topology

```
Bybit public WS
  publicTrade.*
  orderbook.1.*
  orderbook.50.*
  tickers.*
        |
        v
 MARKET ADAPTER
 timestamp normalization
 local L2 reconstruction
 feed health
        |
        v
 FEATURE ENGINE (shared)
 return windows
 trade velocity
 signed volume / buy pressure
 spread
 depth imbalance
 book depletion
 volatility
 acceleration
 funding / OI context
        |
        +-------------------+-------------------+
        v                   v                   v
 HEAD-LIQUID           HEAD-VOL            HEAD-LAUNCH
 SOL/ETH/BTC            volatile alts        pre-market / innovation
 own gates              own gates            own gates
 own risk               own risk             own risk
 own exits              own exits            own exits
        ___________________|___________________/
                            |
                            v
                    PAPER EXECUTOR
                 orderbook/VWAP fills
                            |
                            v
                 CAPITAL + TRADE LEDGER
                  TRADING / PROFIT wallet
                            |
                            v
                       DASHBOARD
```

## 2. One head or many?

### Decision: 3 heads, one machine

Do not create one head per coin and do not create one head per exchange.

A **head** means one coherent strategy/risk model for a market microstructure class.

### HEAD-LIQUID
Universe: SOL, ETH, BTC initially.

Characteristics:
- tight spread;
- deep book;
- lower slippage;
- fewer extreme tails;
- signal quality should dominate raw speed.

Initial role: calibration/control head.

### HEAD-VOL
Universe: standard perpetuals selected dynamically by realized volatility + volume + minimum depth/spread quality.

Characteristics:
- closer to memecoin movement;
- larger tail opportunity;
- higher adverse selection and book thinning.

This is the primary BBB alpha head.

### HEAD-LAUNCH
Universe: Bybit Pre-Market Perpetual and Innovation Zone.

Characteristics:
- materially higher fee floor;
- wider/unstable spread;
- launch regime changes;
- separate funding/transition mechanics.

This head must never silently fall back to LIQUID/VOL parameters.

## 3. Shared signal engine, head-specific policy

Shared features:
- return_100ms / 250ms / 500ms / 1s / 2s / 5s;
- trade_count velocity;
- quote velocity;
- taker buy/sell imbalance;
- top-of-book spread bps;
- L1/L5/L20 depth imbalance;
- depth consumed in recent window;
- short realized volatility;
- acceleration;
- distance from local micro-VWAP;
- mark/index basis;
- funding and time-to-funding;
- open-interest delta when available.

Each head owns:
- score weights;
- hard gates;
- cooldown;
- min edge over costs;
- position sizing;
- max simultaneous positions;
- stop/ELC logic;
- tail/runner logic;
- time exit.

## 4. Two-wallet accounting

### TRADING WALLET
Capital allowed to take risk.

Displayed:
- Start
- Cash
- Open cost/notional exposure
- Trading equity
- Sizing bank
- Working cash reserve
- Realized PnL
- Unrealized PnL
- Fees
- Funding
- Slippage/impact
- MaxDD

### PROFIT WALLET
Reserved realized profit removed from sizing capital.

Invariant:
```
total_equity = trading_equity + profit_wallet
```

A skim is an internal ledger transfer, not a fake expense.

Heads receive virtual sub-ledgers for attribution:
- allocated_risk
- realized_pnl
- unrealized_pnl
- fees
- funding
- slippage
- max_dd
- win/loss

But there is only one global pair of wallets to avoid hidden double-counting of capital.

## 5. Signal/state indicators

Dashboard top strip:

### Market/data state
- BYBIT PUBLIC WS: GREEN/YELLOW/RED
- TRADE FEED AGE p50/p99
- BOOK FEED AGE p50/p99
- BOOK SYNC: SYNCED/RESYNC
- RECONNECTS
- DROPPED EVENTS

### Machine state
- PAPER ONLY
- SESSION / EXPERIMENT ID
- runtime
- processing lag p50/p99
- event queue depth
- SQLite persistence heartbeat

### Per-head signal state
- ACTIVE / GATED / COOLDOWN / NO_MARKET
- last score / gate
- cost floor bps
- spread bps
- volatility state
- risk multiplier
- CB READY/BLOCKED
- last reject reason
- current symbol universe count

## 6. Position lifecycle

```
CANDIDATE
 -> SIGNAL
 -> PENDING_ENTRY
 -> OPEN
 -> EXIT_ARMED
 -> PENDING_EXIT
 -> CLOSED
```

Every transition is an event row. No position can disappear by mutation alone.

## 7. Trade record

Closed trade must retain entry-time attribution, not reconstructed later:

- session_id / experiment_id
- head
- venue / product class / symbol
- side
- leverage
- margin_usd / notional_usd
- signal exchange timestamp
- local receive timestamp
- signal timestamp
- paper/live order timestamp
- fill timestamp
- signal mid / bid / ask
- expected entry VWAP
- actual/model entry VWAP
- entry fee
- entry spread cost
- entry impact
- funding paid/received
- exit reason
- exit fee
- exit spread cost
- exit impact
- gross PnL
- net PnL
- MAE / MFE
- holding time
- source_age_ms
- processing_ms
- signal_to_fill_ms
- score / gate / risk state
- complete feature snapshot.

## 8. Paper-fill rule

PAPER must not fill at last trade.

For an immediate BUY:
- walk asks until requested notional is filled;
- compute VWAP;
- add taker fee;
- reject/partial if configured depth guard fails.

For SELL:
- walk bids analogously.

This makes spread + depth impact endogenous to the paper model.

## 9. Safety split

`MarketAdapter` knows exchange wire format.
`FeatureEngine` knows normalized market events.
`StrategyHead` decides.
`PaperExecutor` models fills.
`CapitalLedger` owns money.
`TradeLedger` owns history.

No strategy head is allowed to mutate wallet balances directly.

## 10. First paper protocol

Phase A — feed only, 2h:
- no entries;
- collect feed age, spread/depth distributions and reconnects.

Phase B — shadow signals, 6h:
- all 3 heads score;
- no fills;
- compare candidate frequency and expected cost floor.

Phase C — PAPER fills, 12–24h:
- independent head attribution;
- same starting global bank;
- no LIVE.

Promotion evidence:
- enough closed trades per head;
- net PnL after all modeled costs;
- tail capture / MAE-MFE;
- DD;
- latency distribution;
- cost distribution;
- reject reasons.

