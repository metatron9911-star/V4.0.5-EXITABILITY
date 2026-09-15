# Accounts / wallets checklist

1. GitHub: private repo `V4-momentum`.
2. Railway: service from repo; Volume `/data`; secrets only in Railway Variables.
3. Birdeye: API key; put only in Railway `BIRDEYE_API_KEY`.
4. Helius: API key; reserve for V4 realtime transaction stream/RPC.
5. Telegram: create a dedicated bot via BotFather; save token as Railway secret; use only alerts in V4 PAPER.
6. Phantom: keep main custody wallet separate. Create a dedicated V4 Trading Wallet with only the permitted bankroll + small SOL gas reserve.
7. Fiat/CEX bridge: WhiteBIT is a practical Ukraine-oriented candidate because its official help documents UAH card-transfer/P2P Express and crypto withdrawals. KYC/fees/network availability must be checked at funding time.

NEVER:
- commit seed phrases/private keys/API secrets to GitHub;
- give V4 access to the main custody wallet;
- enable AUTO in this build;
- assume a displayed token price is executable without a quote/liquidity check.
