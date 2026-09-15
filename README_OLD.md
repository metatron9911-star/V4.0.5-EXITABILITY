# V4 Momentum Hunter — PAPER

Purpose: discover Solana momentum candidates, score momentum/risk, simulate a $150 position, and manage it with the Moonbag Ladder.

## Moonbag Ladder
- Before first +100%: provisional stop = -20% (tune by backtest).
- At +100%: harvest 50% of embedded profit.
- After first milestone: trailing full exit at -50% from peak.
- At +1000%, +3000%, +10000%: harvest 50% of embedded profit again.
- PAPER friction defaults to 1.5% per sale + $0.05 network proxy.

## Safety
This package cannot place real orders. `execution_DISABLED.py` always raises. `AUTO_TRADING_ENABLED=false` and `auto_trading_hard_lock=true` are additional locks.

## Railway
1. Push all files to a private GitHub repo.
2. New Railway service from repo.
3. Add Volume mounted at `/data` and variable `DATA_DIR=/data`.
4. Variables: `BIRDEYE_API_KEY`, optionally `HELIUS_API_KEY`, Telegram credentials.
5. Keep `V4_MODE=PAPER` and `AUTO_TRADING_ENABLED=false`.
6. Generate service domain.

## Accounts
- Birdeye API account/key — discovery and realtime market data.
- Helius account/key — Solana realtime/RPC later; not required for the seed scanner.
- GitHub private repo — source.
- Railway — deployment + volume + secrets.
- Telegram BotFather — optional alerts/control later.
- Phantom — human custody/UI; create a separate V4 trading wallet. Do not expose main-wallet seed to server.
- WhiteBIT or another compliant fiat/CEX route — funding/off-ramp. Transfer only the working bankroll to the Solana wallet.

## V4.1 gate
Do not add execution until PAPER has enough observations and passes explicit acceptance criteria (positive net expectancy after realistic executable quotes/slippage, acceptable drawdown, and no dependence on a few survivor outliers).


## v4.0.1 Telegram integration
- Dashboard button: **Test Telegram**.
- PAPER BUY / HARVEST / EXIT events send Telegram notifications when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured.
- AUTO execution remains physically disabled. `SOLANA_PRIVATE_KEY_B58` is not used by this build.
