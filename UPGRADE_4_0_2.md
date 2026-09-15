# Upgrade v4.0.1 → v4.0.2

1. In GitHub repository `V4-momentum`, replace `app.py`, `engine.py`, `telegram_notify.py`, `config.json`.
2. Add the new file `monitoring.py`.
3. Do not rename the files; Railway Procfile starts `app.py`.
4. Railway auto-deploys.
5. Optional Railway variables:
   - `AUTO_SCAN_INTERVAL_SEC=30`
   - `BIRDEYE_POLL_INTERVAL_SEC=1800`
   - `STATUS_HEARTBEAT_MINUTES=60`
6. Leave `SOLANA_PRIVATE_KEY_B58` empty.
7. Open dashboard → verify all source/module indicators.
8. Telegram: send `/status` to the bot to request an immediate report.
