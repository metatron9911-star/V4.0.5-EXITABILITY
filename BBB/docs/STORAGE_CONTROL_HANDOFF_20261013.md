# BBB STORAGE CONTROL — VERIFIED 2026-10-13 17:50 UTC

Code SHA4124c4abb0c060f13cb5a76b1b8b3cf0d86f7ac9.
Branch maintenance/bbb-storage-control-20261013; draft PR#1.
Railway deployment e52260c0-405c-40fc-aa85-863e30703e4f SUCCESS, pinned to code SHA.
Baseline bbb-h3t-c36b26f8bc81a418c3d66bbc934b088af35ad2a13 preserved; no strategy/wallet/secret/HALT/RP7 changes, no volume resize.

## Before / after and metric scope
Before17:23:00 logical /data files5112388 bytes, Railway service disk approximately205–206MB. After17:50:00 logical files6468212 bytes; actual mounted used6729728 bytes; available4884844544 bytes; deleted-open bytes0 and unknown0.
Railway DISK_USAGE_GB measures service disk, not this volume's exact free space. New diagnostic separately measures filesystem used/available and logical/allocated file totals. Actual /data is roughly6.7MB, not full. No evidence justifies another BBB wipe.
Actual reclaimed bytes in this task:0. No eligible owned temporary files were found/deleted.

## Working controls
Existing warning150000000 and hard250000000 logical bytes; new mounted-free reserve64MiB; largest-file warning64MB; growth warning5MB/min. max SQLite pages128MB retained. Unknown/symlink/stat/free-space failure stops persistence rather than guessing.
Every30s housekeeping may delete ONLY storage_guard_tmp/guard-*.tmp <=256KiB, older1h, with exact completed header BBB_GUARD_TEMP_V1 COMPLETE. No other file is deemed temporary by name.
Core database, WAL/SHM, config, trading/wallet history and valuable research outputs are never file-evicted. Existing market persistence remains0; research microflow cap2000/council cap3000, payload12KiB/softstop25000000 remain. Removed automatic trades/wallet_ledger row-eviction triggers; existing bounded research sample caps retained. Existing journal mode is observed, never switched/checkpointed/VACUUMed by this change.

## Actual running proof
17:50:00 stateOK market_persist_enabled0 live_submission_enabled0, feedCONNECTED.
housekeeping_runs23; owned_temp_removed0.
growth1695.13 bytes/sec (recent samples0–2177.14); council rows3000 across consecutive samples, microflow844. events/trades/wallet/equity/feature rows0.
Growth is bounded research sample accumulation/database metadata and normal SQLite allocation, not evidence of a disk-full incident. This short window is not a permanent growth forecast.
Production periodic control and mounted diagnostics are proven. Actual eligible-temp deletion was NOT observed in production; six regression tests prove its allowlist/age/completion restrictions.

## Validation
6 storage-guard tests PASS locally and on exact source CI.
Actions38439831462 push and38439839904 PR SUCCESS.
Tests cover telemetry OFF, restart/caps/hard guard, low free-space stop, only complete aged owned-temp deletion, absence of trade/wallet auto-eviction triggers, growth arithmetic.
Next action: monitor sampled mounted usage/free and protected research growth; do not repeat blanket cleanup. If protected data reaches hard/reserve guard, retain stop and diagnose exact category.
