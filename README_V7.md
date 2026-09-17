# V7.2 FREQUENT / ADAPTIVE PAPER

PAPER-only build. Real execution remains disabled.

Changes from V7.1:
- Excel download is generated directly in the dashboard; no two-step prepare/download state.
- Up to 50 concurrent small positions; sizing is adaptive (base ~2.5% equity, stronger validated signal up to 5%, hard cap 6%).
- Scanner target interval 5 s; open-position executable quote monitor target interval 1 s. These are scheduler targets, not guaranteed market-data latency.
- Frequent exit: full PAPER close when executable Jupiter minOut implies >=3% net profit versus recorded position cost; stop remains -2% trigger.
- Pool/route trading fees and slippage already reflected in executable quote minOut. A separate configurable network-fee estimate remains $0.01 per transaction for PAPER accounting.
- Hourly Telegram status retained.
- New database v7_2.sqlite3 preserves V7.1 history.

Important: HTTP polling cannot be truthfully described as millisecond trading. For sub-second event-driven operation use WebSocket/streaming market data and benchmark end-to-end latency before enabling any real execution. Profit is not guaranteed; PAPER results must include all costs and failed/unavailable routes.
