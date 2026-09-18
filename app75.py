"""V7.9 dashboard wrapper: isolated V7.9 state, WAVE+PEAK-LOCK experiment."""
import os
from pathlib import Path
os.environ['DATA_DIR']=os.path.join(os.getenv('DATA_DIR','/data'),'v79')
src=Path(__file__).with_name('app.py').read_text(encoding='utf-8')
src=src.replace("V7.4 FAST CORE PAPER","V7.9 WAVE+PEAK-LOCK PAPER")
src=src.replace("⚡ V7.4 — FAST CORE PAPER","🚀 V7.9 — WAVE + PEAK-LOCK PAPER")
src=src.replace("V7.4 FAST CORE — PAPER","V7.7 WAVE+PEAK-LOCK — PAPER")
src=src.replace("V7_4_FAST_CORE_","V7_9_WAVE_")
src=src.replace("PAPER: реальные сделки отключены. Быстрый входной контур, адаптивные ставки, до 50 позиций. Критические проверки безопасности сохранены; RugCheck warning допускается и протоколируется, danger блокируется.","PAPER: реальные сделки отключены. Три контура: Pump.fun WSS ловит создание монеты сразу, bonding curve торгуется ДО DEX, DEX/Jupiter работает ПОСЛЕ появления маршрута. При graduation выход автоматически переключается на Jupiter/PumpSwap. До 50 адаптивных позиций.")
src=src.replace("'PAIR_UNAVAILABLE':'Пара DEX не найдена'","'PAIR_UNAVAILABLE':'Пара DEX не найдена','CURVE_OPENED':'РАННИЙ PUMP ВХОД ОТКРЫТ','CURVE_WARMUP':'Bonding curve: набираем вторую точку','CURVE_AGE_WAIT':'Bonding curve: ждём возраст','CURVE_MC_LOW':'Bonding curve: малая капитализация','CURVE_TRADE_STALE':'Bonding curve: давно не было сделки','CURVE_MOMENTUM_WAIT':'Bonding curve: ждём положительный импульс','CURVE_SECURITY_PASSED':'Bonding curve: критические проверки пройдены','CURVE_ROUNDTRIP_COST_HIGH':'Bonding curve: round-trip слишком дорогой'")
src=src.replace("stage IN ('ENTRY','DECISION','RISK','SECURITY','SECURITY_CACHE','SECURITY_CURVE','CURVE','STREAM','MARKET','WATCH')","stage IN ('ENTRY','DECISION','RISK','SECURITY','SECURITY_CACHE','SECURITY_CURVE','CURVE','MARKET','WATCH')")
src=src.replace("priority={'ENTRY':8,'DECISION':7,'SECURITY_CURVE':6,'SECURITY':5,'SECURITY_CACHE':5,'STREAM':4,'RISK':3,'CURVE':2,'WATCH':2,'MARKET':1}","priority={'ENTRY':8,'DECISION':7,'SECURITY_CURVE':6,'SECURITY':5,'SECURITY_CACHE':5,'RISK':3,'CURVE':2,'WATCH':2,'MARKET':1}")
src=src.replace("reason=='COHORT_OPENED'","reason in ('COHORT_OPENED','CURVE_OPENED')")
src=src.replace("'SECURITY_PASSED_WITH_WARNING','ADAPTIVE_RISK_OK')","'SECURITY_PASSED_WITH_WARNING','CURVE_SECURITY_PASSED','ADAPTIVE_RISK_OK')")
exec(compile(src,'app75.py','exec'),globals(),globals())
