# BBB research handoff before destructive Railway purge — 2026-10-13

## Purpose

Compact permanent handoff of the only BBB/Bybit research results worth keeping
before deleting the full Railway `/data` market store.

The raw SQLite market store is intentionally **not** preserved.

## Storage failure

Railway service: `BBB`
Volume: `bbb-data`
Mount: `/data`
Capacity: 5000 MB

Observed immediately before purge:

- `/data/bbb.sqlite3`: 4,704,288,768 bytes
- `/data/bbb.sqlite3-wal`: 186,833,792 bytes
- `/data/bbb.sqlite3-shm`: 28,672 bytes
- Total observed storage: 4,891,305,425 bytes
- Normal read-only SQLite open: `disk I/O error`
- immutable SQLite open: `database disk image is malformed`

Conclusion: do not spend volume space trying to repair or clone this database.
Delete it after this handoff.

## Legacy ROCKET result

Own BBB/Bybit trade history:

- 95 closed trades
- 10 WIN
- 85 LOSS
- base WR 10.5263%

The old ROCKET selector is rejected.

## Market-wide WIN-DNA V2

Source: BBB retained Bybit feature snapshots, no legacy ROCKET gate.
Recent 300k raw samples produced 32,445 decision points.

Candidate portrait:

- `trade_count_5s >= 203`
- `live_activity_score >= 117.0219576`
- `d10_trade_count_5s >= 63`
- 3/3 required

Chronological holdout:

- FAST precision 20.3614%, lift 2.0841
- QUALITY precision 14.7239%, lift 2.4439
- STRONG precision 11.4120%, lift 2.3995

Interpretation: this is an activity/enrichment filter, **not a WIN-DNA filter**.
Too many losses pass. Rejected for trading.

## WIN-DNA V3 — high precision / hard negatives

Source: 300k BBB/Bybit raw feature samples.

- 5,857 independent episodes
- 58 features
- chronological split:
  - discovery 3,514
  - validation 1,171
  - blind test 1,172
- WIN base rates:
  - discovery 4.8093%
  - validation 5.3800%
  - blind 5.7167%

Strongest single features were mostly reversal/exhaustion-like:
negative short return, negative return acceleration, activity bursts and changing
trade velocity.

Examples:

- `ret_accel <= -4.3142`: validation precision 15.0442%
- `r250 <= -1.4817`: validation precision 15.8879%
- `d10_t5 >= 104.3`: validation precision 13.9344%

No validated high-precision combination survived the defined gate.

Decision:
`NO_HIGH_PRECISION_SELECTOR_FOUND`

Gate that was deliberately required:
blind precision >= 45%, >=10 signals, >=5 WIN.

## WIN-DNA V4 — trajectory archetypes

Source: same BBB/Bybit history, now using sequence shapes rather than one snapshot.

- 300,000 raw rows
- 23 symbols
- 5,748 independent episodes
- pre-decision offsets: -20, -15, -10, -5, -2, 0 sec
- discovery / validation / blind = 3,448 / 1,150 / 1,150
- WIN counts = 159 / 66 / 62

WIN trajectories were clustered and matched against similar LOSS contexts.

The most interesting discovery archetype showed:

- very elevated activity 15–20s before the candidate,
- very high trade_count_5s,
- later loss of momentum / negative short return,
- plausible shock -> exhaustion / reversal shape.

But it did not generalize.

Blind results by main archetypes included:

- archetype 1: 1 WIN / 14 LOSS
- archetype 3: 0 WIN / 11 LOSS
- archetype 2: 0 WIN / 1 LOSS

Combined blind:

- 27 signals
- 1 WIN
- 26 LOSS
- precision 3.7037%

Decision:
`RESEARCH_COMPLETE_DO_NOT_TRADE_AUTOMATICALLY`

## Key scientific conclusion

The retained 1-second aggregate snapshots are not sufficient to separate future
WIN from hard matched LOSS with useful precision.

The next information layer must be the **microflow between snapshots**:

- individual publicTrade events,
- buyer/seller notional burst structure,
- run lengths and side switching,
- L1/L5/L50 bid/ask liquidity appearance and withdrawal,
- imbalance change,
- spread change,
- price impact per unit aggressor flow,
- absorption,
- exhaustion,
- transition timing.

## Dynamic microflow filter already implemented in code

Files retained in GitHub:

- `BBB/microflow_dynamic.py`
- `BBB/broker_council.py`

State machine:

`SELL_SHOCK -> ABSORB -> TURN -> 2-frame CONFIRM`

Evaluation interval: approximately 250 ms.

The system is SHADOW/research only.

### Electronic broker council

Independent agents:

1. TAPE
2. BOOK
3. IMPULSE
4. ABSORPTION
5. REGIME
6. RISK_VETO

Coordinator outputs:

- SUPPORT
- WAIT
- REJECT
- VETO

It stores each agent's score/confidence/evidence, regime-dependent weights,
agreement/dispersion, veto reasons and final coordinator score.

## Current safety state

Legacy trading entries must remain disabled during cleanup/restart:

- `BBB_ROCKET_ENTRY_ENABLED=0`
- `BBB_MARKET_DNA_ENTRY_ENABLED=0`

Do not infer any LIVE authorization from this handoff.

## Data intentionally disposable

The following are **not** required to preserve:

- old `bbb.sqlite3`
- WAL / SHM
- feature_samples
- market sample events
- old session telemetry
- old trades / ledgers from this BBB research epoch
- raw microflow/council rows trapped inside the malformed database
- generated research JSON files on the volume once this handoff is committed

After deletion, future research must use a bounded recorder with hard retention
and storage caps so the volume cannot grow without bound again.
