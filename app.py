import time
import streamlit as st
from engine import Store, export_excel, now_ms
from telegram_status import configured, status_text, send

st.set_page_config(page_title='V7.4 FAST CORE PAPER',layout='wide')
st.title('⚡ V7.4 — FAST CORE PAPER')
st.info('PAPER: реальные сделки отключены. Быстрый входной контур, адаптивные ставки, до 50 позиций. Критические проверки безопасности сохранены; RugCheck warning допускается и протоколируется, danger блокируется.')
@st.cache_resource
def connect():return Store()
try:db=connect()
except Exception as exc:st.error(str(exc));st.stop()
REASON_RU={'PASS':'Рыночные фильтры пройдены','COHORT_OPENED':'ВХОД ОТКРЫТ','ADAPTIVE_RISK_OK':'Адаптивный размер ставки разрешён','LOW_LIQUIDITY':'Недостаточная ликвидность','LOW_VOLUME':'Недостаточный объём за 5 мин','LOW_SELL_ACTIVITY':'Мало продаж за 5 мин','BUY_FLOW_WEAK':'Покупок не больше, чем продаж','PAIR_TOO_NEW':'Пара ещё слишком новая','MOMENTUM_OUTSIDE_RANGE':'Импульс вне диапазона','PAIR_UNAVAILABLE':'Пара DEX не найдена','INVALID_PAIR':'Некорректные данные пары','RESET_UNSTABLE':'Движение нестабильно','WAIT':'Ждём подтверждения','CONFIRMED':'Сигнал подтверждён','POSITION_LIMIT':'Все слоты заняты','EXPERIMENT_LOSS_PAUSE':'Лимит потерь эксперимента','COST_EXCEEDS_BUDGET':'Расходы слишком велики','ACTIVE_AUTHORITY':'Mint/freeze authority активна','LARGE_TOKEN_ACCOUNT_INCLUDING_POOLS':'Слишком крупный token account','LP_LOCK_LOW':'Недостаточная блокировка LP','RUGCHECK_DANGER':'RugCheck: DANGER — вход запрещён','SECURITY_PASSED_WITH_WARNING':'RugCheck: warning — допущено после критических проверок','SECURITY_SCREEN_PASSED':'Проверка безопасности пройдена','SECURITY_UNAVAILABLE':'Проверка безопасности недоступна','BUY_QUOTE_UNAVAILABLE':'Нет свежей котировки на вход','ROUNDTRIP_QUOTE_UNAVAILABLE':'Нет маршрута вход→выход','ROUNDTRIP_COST_HIGH':'Слишком дорогой round-trip','IMPACT_HIGH':'Высокий price impact','SOURCE_DIVERGENCE':'Расхождение цены источника и котировки','NET_STOP_LOSS':'Закрытие по стопу','NET_TAKE_PROFIT':'Фиксация чистой PAPER-прибыли','QUOTE_UNAVAILABLE':'Нет свежей котировки на выход'}
def ru(x):return '; '.join(REASON_RU.get(p,p) for p in str(x or '').split('|'))
def tm(ms):return time.strftime('%H:%M:%S',time.localtime(ms/1000)) if ms else ''
def indicator(label,ok,detail):st.markdown(f"**{'🟢' if ok else '🔴'} {label}**  \n{detail}")
cols=st.columns(4)
if cols[0].button('🔄 Refresh',width="stretch"):st.rerun()
if cols[1].button('🔎 Scan now',width="stretch"):
    i,s=db.request_scan();st.success(f'Сканирование #{i}: {s}')
if cols[2].button('✈️ Telegram status',width="stretch"):
    ok,d=send(status_text(db,db.snapshot()));(st.success if ok else st.error)(d)
if cols[3].button('🧪 Test Telegram',width="stretch"):
    ok,d=send('V7.4 FAST CORE — PAPER\nПроверка Telegram. REAL execution OFF.');(st.success if ok else st.error)(d)
@st.fragment(run_every='15s')
def dashboard():
    s=db.snapshot();now=now_ms()
    with db.lock:
        last_discovery=db.cx.execute("SELECT ts_ms FROM events WHERE stage='DISCOVERY' ORDER BY id DESC LIMIT 1").fetchone(); last_report=db.cx.execute("SELECT ts_ms,reason FROM events WHERE stage='TELEGRAM_REPORT' ORDER BY id DESC LIMIT 1").fetchone()
    st.caption('Эксперимент: '+s['config']['experiment']+' · '+s['config_hash'][:12])
    st.subheader('Состояние машинки');cards=st.columns(6);hb=s.get('heartbeat',{});scan=hb.get('scanner');ex=hb.get('exits');pump=hb.get('pump_wss')
    with cards[0]:indicator('Scanner',bool(scan and now-scan['ts_ms']<60000),scan.get('status','NO HEARTBEAT') if scan else 'NO HEARTBEAT')
    with cards[1]:indicator('Exit monitor',bool(ex and now-ex['ts_ms']<30000),ex.get('status','NO HEARTBEAT') if ex else 'NO HEARTBEAT')
    with cards[2]:indicator('Pump WSS',bool(pump and now-pump['ts_ms']<60000),pump.get('status','NO HEARTBEAT') if pump else 'NO HEARTBEAT')
    with cards[3]:indicator('Discovery',bool(last_discovery and now-last_discovery[0]<120000),'свежий проход' if last_discovery else 'нет данных')
    with cards[4]:indicator('Telegram',configured(),'настроен; отчёт каждый час'+(f' · {tm(last_report[0])} {last_report[1]}' if last_report else ''))
    with cards[5]:indicator('PAPER / adaptive',True,f"до {s['config']['max_open_positions']} позиций · REAL execution OFF")
    st.subheader('Банк и результат')
    stats_rows=db.stats(s)
    if stats_rows:
        first=stats_rows[0]
        equity_raw=first.get('equity'); net_raw=first.get('net_pnl')
        equity=float(equity_raw) if equity_raw is not None else float(s['config'].get('paper_capital_usd',150))
        net=float(net_raw) if net_raw is not None else equity-float(s['config'].get('paper_capital_usd',150))
        state='🟢' if net>0 else ('🔴' if net<0 else '⚪')
        st.metric(f"{state} Баланс после всех комиссий",f"$"+f"{equity:.4f}",delta=f"{net:+.4f}")
    st.dataframe(stats_rows,hide_index=True,width="stretch")
    st.subheader('Открытые позиции');rows=[]
    for p in s['positions']:
        if int(p['remaining_raw']):
            pnl=None if p['mark_net'] is None else p['received']+p['mark_net']-p['cost'];rows.append({'монета':p['symbol'],'mint':p['mint'],'вход':tm(p['entry_ms']),'ставка_$':round(p['cost'],4),'текущее_PNL_$':None if pnl is None else round(pnl,4),'ожидает_выход':ru(p.get('pending'))})
    st.dataframe(rows,width="stretch",hide_index=True)
    st.subheader('Входы и причины отказов — последние решения')
    with db.lock:ev=db.cx.execute("SELECT ts_ms,mint,stage,reason FROM events WHERE stage IN ('ENTRY','DECISION','RISK','SECURITY','SECURITY_CACHE','STREAM','MARKET','WATCH') ORDER BY id DESC LIMIT 120").fetchall()
    priority={'ENTRY':8,'DECISION':7,'SECURITY':6,'SECURITY_CACHE':6,'STREAM':4,'RISK':3,'WATCH':2,'MARKET':1};latest={}
    for ts,mint,stage,reason in ev:
        if not mint:continue
        if mint not in latest or priority.get(stage,0)>latest[mint]['p']:latest[mint]={'время':tm(ts),'mint':mint,'этап':stage,'статус':'ВХОД' if reason=='COHORT_OPENED' else ('ПРОШЁЛ' if reason in ('PASS','CONFIRMED','SECURITY_SCREEN_PASSED','SECURITY_PASSED_WITH_WARNING','ADAPTIVE_RISK_OK') else 'ОТКАЗ/ОЖИДАНИЕ'),'причина':ru(reason),'p':priority.get(stage,0)}
    out=[]
    for x in latest.values():y=x.copy();y.pop('p');out.append(y)
    st.dataframe(out[:40],width="stretch",hide_index=True)
    st.subheader('Сводка причин отказов')
    with db.lock:r=db.cx.execute("SELECT stage,reason,COUNT(*) FROM events WHERE ts_ms>=? AND stage IN ('MARKET','WATCH','RISK','SECURITY','SECURITY_CACHE','SECURITY_CURVE','CURVE','STREAM','DECISION') AND reason NOT IN ('PASS','WAIT','CONFIRMED','ADAPTIVE_RISK_OK','SECURITY_SCREEN_PASSED','SECURITY_PASSED_WITH_WARNING','CURVE_SECURITY_PASSED','COHORT_OPENED','CURVE_OPENED') GROUP BY stage,reason ORDER BY COUNT(*) DESC LIMIT 40",(now-3600000,)).fetchall()
    st.dataframe([{'этап':a,'причина':ru(b),'код':b,'сколько':c} for a,b,c in r],width="stretch",hide_index=True)
dashboard()
periods={'1 час':1,'3 часа':3,'6 часов':6,'12 часов':12,'24 часа':24,'48 часов':48,'7 дней':168,'Весь эксперимент':None};period=st.selectbox('Период Excel-отчёта',list(periods),index=0)
st.caption('Excel формируется только по нажатию — страница больше не пересобирает тяжёлый файл при каждом обновлении.')
try:
    if st.button('🧾 Подготовить Excel',width="stretch"):
        with st.spinner('Формирую отчёт…'):
            st.session_state['v7_excel']=export_excel(db,periods[period])
            st.session_state['v7_excel_name']=f'V7_8_STREAM_{periods[period] or "all"}h_{now_ms()}.xlsx'
    if st.session_state.get('v7_excel'):
        st.download_button('📥 Скачать готовый Excel',st.session_state['v7_excel'],st.session_state.get('v7_excel_name','V7_8.xlsx'),mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',width="stretch")
except Exception as exc:
    st.error('Excel не сформирован: '+type(exc).__name__+' — '+str(exc))
