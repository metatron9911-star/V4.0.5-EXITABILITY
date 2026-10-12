PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  exchange_ts_ms INTEGER,
  session_id TEXT NOT NULL,
  head TEXT,
  symbol TEXT,
  stage TEXT NOT NULL,
  reason TEXT,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  position_id TEXT NOT NULL UNIQUE,
  head TEXT NOT NULL,
  venue TEXT NOT NULL,
  product_class TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  leverage REAL NOT NULL,
  margin_usd REAL NOT NULL,
  notional_usd REAL NOT NULL,

  signal_exchange_ts_ms INTEGER,
  receive_ts_ms INTEGER,
  signal_ts_ms INTEGER,
  order_ts_ms INTEGER,
  fill_ts_ms INTEGER,

  signal_mid REAL,
  signal_bid REAL,
  signal_ask REAL,
  expected_entry_vwap REAL,
  entry_vwap REAL,
  exit_vwap REAL,

  entry_fee_usd REAL DEFAULT 0,
  exit_fee_usd REAL DEFAULT 0,
  funding_usd REAL DEFAULT 0,
  spread_impact_usd REAL DEFAULT 0,
  latency_cost_usd REAL DEFAULT 0,

  gross_pnl_usd REAL DEFAULT 0,
  net_pnl_usd REAL DEFAULT 0,
  mae_pct REAL,
  mfe_pct REAL,
  hold_ms INTEGER,

  source_age_ms REAL,
  processing_ms REAL,
  signal_to_fill_ms REAL,

  entry_score REAL,
  entry_gate REAL,
  risk_state TEXT,
  entry_features_json TEXT NOT NULL,

  exit_reason TEXT,
  status TEXT NOT NULL,
  opened_ms INTEGER NOT NULL,
  closed_ms INTEGER
);

CREATE TABLE IF NOT EXISTS wallet_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  wallet TEXT NOT NULL CHECK(wallet IN ('TRADING','PROFIT')),
  head TEXT,
  position_id TEXT,
  kind TEXT NOT NULL,
  amount_usd REAL NOT NULL,
  balance_after_usd REAL NOT NULL,
  detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  trading_equity_usd REAL NOT NULL,
  profit_wallet_usd REAL NOT NULL,
  total_equity_usd REAL NOT NULL,
  open_positions INTEGER NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session_ts ON events(session_id, ts_ms);
CREATE INDEX IF NOT EXISTS idx_trades_head_closed ON trades(head, closed_ms);
CREATE INDEX IF NOT EXISTS idx_ledger_session_ts ON wallet_ledger(session_id, ts_ms);


CREATE TABLE IF NOT EXISTS feature_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  exchange_ts_ms INTEGER,
  session_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  head TEXT NOT NULL,
  mid REAL,
  bid REAL,
  ask REAL,
  spread_bps REAL,
  depth_bid_5_usd REAL,
  depth_ask_5_usd REAL,
  book_imbalance_5 REAL,
  trade_count_1s INTEGER,
  trade_count_5s INTEGER,
  buy_ratio_1s REAL,
  ret_250ms_bps REAL,
  ret_1s_bps REAL,
  ret_5s_bps REAL,
  realized_vol_5s_bps REAL,
  score REAL,
  gate REAL,
  state TEXT,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_features_symbol_ts ON feature_samples(symbol, ts_ms);


CREATE TABLE IF NOT EXISTS microflow_episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  shock_ms INTEGER NOT NULL,
  decision_ms INTEGER NOT NULL,
  completed_ms INTEGER NOT NULL,
  decision_stage TEXT NOT NULL,
  outcome_label TEXT,
  first_touch_ms INTEGER,
  entry_ask REAL,
  exit_probe_bid REAL,
  max_favorable_bps REAL,
  max_adverse_bps REAL,
  shock_score REAL,
  absorb_score REAL,
  turn_score REAL,
  dynamic_score REAL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_microflow_episode_ts ON microflow_episodes(session_id,decision_ms);
