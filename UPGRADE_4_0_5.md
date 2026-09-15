# V4.0.5 EXITABILITY

Relaxed PAPER gates: liquidity >= $20k, score >= 55, risk <= 60, max 3 paper positions x $50.

Strict new gate: an entry is allowed only when Jupiter returns an executable token->USDC sell route for the simulated position, with price impact <=5% and quoted value >=82% of nominal. This requires JUPITER_API_KEY. No transaction signing/sending code was added; AUTO remains physically disabled.

Replay Lab uses the SQLite snapshots already collected on Railway. Because older V4 snapshots were not guaranteed to follow every candidate continuously, sparse histories are excluded and reported.
