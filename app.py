import json
import time
import streamlit as st
from engine import Store, export_excel, now_ms
from telegram_status import configured, status_text, send

st.set_page_config(page_title='V7.2 PAPER Machine', layout='wide')
st.title('V7.2 — FREQUENT / ADAPTIVE PAPER')
st.info('PAPER: реальные сделки отключены. До 50 небольших позиций; размер ставки адаптивный, а не равный. Выход по прибыли считается по исполнимой котировке после расходов маршрута.')

@st.cache_resource
def connect(): return Store()
try:
    db=connect()
except Exception as exc:
    st.error(str(exc)); st.stop()

REASON_RU={
 'PASS':'Рыночные фильтры пройдены','COHORT_OPENED':'ВХОД ОТКРЫТ','ADAPTIVE_RISK_OK':'Есть свободный слот; адаптивный размер ставки',
 'LOW_LIQUIDITY':'Недостаточная ликвидность','LOW_VOLUME':'Недостаточный объём за 5 мин','LOW_SELL_ACTIVITY':'Мало продаж за 5 мин',
 'BUY_FLOW_WEAK':'Покупок не больше, чем продаж','PAIR_TOO_NEW':'Пара ещё слишком новая','MOMENTUM_OUTSIDE_RANGE':'Импульс вне заданного диапазона',
 'PAIR_UNAVAILABLE':'Пара DEX не найдена','INVALID_PAIR':'Некорректные данные пары','RESET_UNSTABLE':'Подтверждение сброшено: движение нестабильно',
 'WAIT':'Ждём подтверждения','CONFIRMED':'Сигнал подтверждён','POSITION_LIMIT':'Все 30 слотов заняты','UNPRICED_POSITION':'Есть позиция без свежей котировки',
 'EXPERIMENT_LOSS_PAUSE':'Достигнут лимит потерь эксперимента','COST_EXCEEDS_BUDGET':'Бюджет слишком мал относительно расходов',
 'ACTIVE_AUTHORITY':'Mint/freeze authority не отозвана','AUTHORITY_UNKNOWN':'Не удалось подтвердить authority','INVALID_SUPPLY':'Некорректный supply',
 'HOLDERS_UNKNOWN':'Не удалось проверить крупнейшие token accounts','LARGE_TOKEN_ACCOUNT_INCLUDING_POOLS':'Слишком крупный token account (включая пулы)',
 'LP_UNKNOWN':'Не удалось подтвердить LP','LP_LOCK_LOW':'Недостаточная блокировка LP','RISKS_UNKNOWN':'RugCheck не вернул список рисков',
 'RUGCHECK_WARNING':'RugCheck вернул warning/danger','SECURITY_UNAVAILABLE':'Проверка безопасности недоступна','SECURITY_SCREEN_PASSED':'Проверка безопасности пройдена',
 'BUY_QUOTE_UNAVAILABLE':'Нет свежей котировки на вход','QUANTITY_TOO_SMALL':'Слишком маленькое количество токенов','ROUNDTRIP_QUOTE_UNAVAILABLE':'Нет проверяемого маршрута вход→выход',
 'ROUNDTRIP_COST_HIGH':'Слишком большие потери round-trip','IMPACT_HIGH':'Слишком высокий price impact','SOURCE_DIVERGENCE':'Цена источника слишком отличается от исполнимой котировки',
 'RISK_CHANGED':'Бюджет/риск изменился во время проверки','ALREADY_TRADED':'Эта монета уже торговалась','QUOTE_EXPIRED':'Котировка устарела до фиксации входа',
 'STOP_2PCT_FROM_ENTRY':'Закрытие: ниже −2% от стартовой стоимости','NET_TAKE_PROFIT':'Фиксация чистой PAPER-прибыли по исполнимой котировке','PRINCIPAL_RECOVERED_2X':'При +100% возвращено тело ставки','QUOTE_UNAVAILABLE':'Нет свежей котировки на выход'
}

def ru(reason):
    if not reason:return ''
    parts=str(reason).split('|')
    return '; '.join(REASON_RU.get(x,x) for x in parts)

def fmt_time(ms):
    if not ms:return ''
    return time.strftime('%H:%M:%S',time.localtime(ms/1000))

def indicator(label, ok, detail):
    icon='🟢' if ok else '🔴'
    st.markdown(f'**{icon} {label}**  \n{detail}')

cols=st.columns(4)
if cols[0].button('Refresh',use_container_width=True):st.rerun()
if cols[1].button('Scan now',use_container_width=True):
    command_id,status=db.request_scan(); st.success(f'Сканирование #{command_id}: {status}.')
if cols[2].button('Telegram status',use_container_width=True):
    s=db.snapshot(); ok,detail=send(status_text(db,s)); (st.success if ok else st.error)(detail)
if cols[3].button('Test Telegram',use_container_width=True):
    ok,detail=send('V7.2 — PAPER\nПроверка Telegram. Реальная торговля отключена.'); (st.success if ok else st.error)(detail)

@st.fragment(run_every='10s')
def dashboard():
    s=db.snapshot(); now=now_ms()
    with db.lock:
        command=db.cx.execute('SELECT id,status FROM commands ORDER BY id DESC LIMIT 1').fetchone()
        last_discovery=db.cx.execute("SELECT ts_ms,reason,payload FROM events WHERE stage='DISCOVERY' ORDER BY id DESC LIMIT 1").fetchone()
        last_report=db.cx.execute("SELECT ts_ms,reason FROM events WHERE stage='TELEGRAM_REPORT' ORDER BY id DESC LIMIT 1").fetchone()
    st.caption('Эксперимент: '+s['config']['experiment']+' · '+s['config_hash'][:12]+(f' · Scan now #{command[0]} {command[1]}' if command else ''))
    if s['paused']: st.warning('Лимит потерь эксперимента достигнут: новые входы остановлены, выходы продолжаются.')

    st.subheader('Состояние машинки')
    cards=st.columns(5)
    hb=s.get('heartbeat',{})
    scan=hb.get('scanner'); exits=hb.get('exits'); tg=hb.get('telegram_report')
    with cards[0]: indicator('Scanner', bool(scan and now-scan['ts_ms']<60000), f"{scan.get('status','NO HEARTBEAT') if scan else 'NO HEARTBEAT'}")
    with cards[1]: indicator('Exit monitor', bool(exits and now-exits['ts_ms']<30000), f"{exits.get('status','NO HEARTBEAT') if exits else 'NO HEARTBEAT'}")
    with cards[2]: indicator('Discovery', bool(last_discovery and now-last_discovery[0]<120000), 'свежий проход' if last_discovery and now-last_discovery[0]<120000 else 'нет свежего прохода')
    with cards[3]: indicator('Telegram', configured(), ('настроен; отчёт каждый час' + (f" · последний {fmt_time(last_report[0])} {last_report[1]}" if last_report else '')) if configured() else 'не заданы token/chat ID')
    with cards[4]: indicator('PAPER / adaptive', s['config']['max_open_positions']>=30, f"до {s['config']['max_open_positions']} позиций · REAL execution OFF")

    stats=db.stats(s)
    st.subheader('Банк и результат')
    st.dataframe(stats,hide_index=True,use_container_width=True)

    st.subheader('Открытые позиции')
    open_rows=[]
    for p in s['positions']:
        if int(p['remaining_raw']):
            pnl=None if p['mark_net'] is None else p['received']+p['mark_net']-p['cost']
            open_rows.append({'монета':p['symbol'],'mint':p['mint'],'вход':fmt_time(p['entry_ms']),'ставка_$':round(p['cost'],4),
                'текущее_PNL_$':None if pnl is None else round(pnl,4),'тело_возвращено': 'ДА' if p['half_done'] else 'нет','ожидает_выход':ru(p.get('pending'))})
    st.dataframe(open_rows,use_container_width=True,hide_index=True)

    st.subheader('Входы и причины отказов — последние решения')
    with db.lock:
        ev=db.cx.execute("SELECT ts_ms,mint,stage,reason,payload FROM events WHERE stage IN ('ENTRY','DECISION','RISK','SECURITY','MARKET','WATCH') ORDER BY id DESC LIMIT 300").fetchall()
    # One readable latest meaningful state per mint; ENTRY/DECISION have priority over intermediate stages.
    priority={'ENTRY':6,'DECISION':5,'SECURITY':4,'RISK':3,'WATCH':2,'MARKET':1}
    latest={}
    for ts,mint,stage,reason,payload in ev:
        if not mint:continue
        cur=latest.get(mint)
        if cur is None or priority.get(stage,0)>cur['_p']:
            status='ВХОД' if reason=='COHORT_OPENED' else ('ПРОШЁЛ' if reason in ('PASS','CONFIRMED','SECURITY_SCREEN_PASSED','ADAPTIVE_RISK_OK') else 'ОТКАЗ/ОЖИДАНИЕ')
            latest[mint]={'время':fmt_time(ts),'mint':mint,'этап':stage,'статус':status,'причина':ru(reason),'_p':priority.get(stage,0)}
    decision_rows=[]
    for x in latest.values():
        x=x.copy();x.pop('_p',None);decision_rows.append(x)
    st.dataframe(decision_rows[:80],use_container_width=True,hide_index=True)

    st.subheader('Сводка причин отказов')
    with db.lock:
        rows=db.cx.execute("SELECT stage,reason,COUNT(*) FROM events WHERE stage IN ('MARKET','WATCH','RISK','SECURITY','DECISION') AND reason NOT IN ('PASS','WAIT','CONFIRMED','ADAPTIVE_RISK_OK','SECURITY_SCREEN_PASSED','COHORT_OPENED') GROUP BY stage,reason ORDER BY COUNT(*) DESC").fetchall()
    st.dataframe([{'этап':a,'причина':ru(b),'код':b,'сколько':c} for a,b,c in rows],use_container_width=True,hide_index=True)

dashboard()
periods={'1 час':1,'3 часа':3,'6 часов':6,'12 часов':12,'24 часа':24,'48 часов':48,'7 дней':168,'Весь эксперимент':None}
period=st.selectbox('Период Excel-отчёта',list(periods),index=0)
st.caption('Excel формируется прямо при обновлении страницы: Summary, Positions, Watch, Config, Events и Equity.')
try:
    xlsx_bytes=export_excel(db,periods[period])
    st.download_button('📥 Скачать Excel сейчас',xlsx_bytes,
        f'V7_2_protocol_{periods[period] or "all"}h_{now_ms()}.xlsx',
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',use_container_width=True)
except Exception as exc:
    st.error('Excel не сформирован: '+type(exc).__name__+' — '+str(exc))
