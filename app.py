import json
import streamlit as st
from engine import Store, export_excel, now_ms
from telegram_status import configured, status_text, send

st.set_page_config(page_title='V6 PAPER Experiment',layout='wide')
st.title('V6 — PAPER experiment')
st.info('Реальные сделки отключены в коде. Два отдельных виртуальных счёта: их балансы не складываются.')
@st.cache_resource
def connect(): return Store()
try: db=connect()
except Exception as exc:
    st.error(str(exc)); st.stop()

cols=st.columns(4)
if cols[0].button('Refresh',use_container_width=True):st.rerun()
if cols[1].button('Scan now',use_container_width=True):
    command_id,status=db.request_scan()
    st.success(f'Сканирование #{command_id}: {status}. Если сканер занят, запрос выполнится после текущего прохода.')
if cols[2].button('Telegram status',use_container_width=True):
    with db.lock:
        s=json.loads(db.cx.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
    ok,detail=send(status_text(db,s))
    (st.success if ok else st.error)(detail)
if cols[3].button('Test Telegram',use_container_width=True):
    ok,detail=send('V6 — PAPER\nПроверка Telegram. Реальная торговля отключена.')
    (st.success if ok else st.error)(detail)
st.caption('Telegram: '+('ключ и chat ID заданы; проверка — кнопкой Test Telegram' if configured() else 'не настроен: TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID'))

@st.fragment(run_every='10s')
def dashboard():
    state=db.cx.execute('SELECT payload FROM state WHERE id=1').fetchone()
    s=json.loads(state[0])
    with db.lock:
        command=db.cx.execute('SELECT id,status FROM commands ORDER BY id DESC LIMIT 1').fetchone()
    if command:st.caption(f'Последний Scan now: #{command[0]} — {command[1]}')
    st.caption('Эксперимент: '+s['config']['experiment']+' · '+s['config_hash'][:12])
    if s['paused']: st.warning('Лимит потерь эксперимента достигнут: новые входы остановлены, выходы продолжаются.')
    for name in ('scanner','exits'):
        h=s['heartbeat'].get(name,{})
        age=(now_ms()-h.get('ts_ms',0))/1000
        if age>60: st.error(f'{name}: нет свежего heartbeat. Проверьте Start Command и Railway logs.')
        else: st.caption(f'{name}: {h.get("status")} · {age:.0f} сек. назад')
    st.dataframe(db.stats(s),hide_index=True,use_container_width=True)
    st.caption('equity / net_pnl пусты, если есть неизвестная или устаревшая стоимость. Комиссии пула уже в котировках; network_fees — отдельная модель расходов.')
    st.subheader('Позиции и выходы')
    st.dataframe(s['positions'],use_container_width=True)
    st.subheader('Подтверждение входов')
    st.dataframe([dict(mint=k,**v) for k,v in s['watch'].items()],use_container_width=True)
    st.subheader('Последние события')
    rows=db.cx.execute('SELECT ts_ms,mint,stage,reason FROM events ORDER BY id DESC LIMIT 100').fetchall()
    st.dataframe([dict(zip(('ts_ms','mint','stage','reason'),r)) for r in rows],use_container_width=True)
    st.subheader('Причины отказов и этапы')
    rows=db.cx.execute('SELECT stage,reason,COUNT(*) FROM events WHERE stage != ? GROUP BY stage,reason ORDER BY COUNT(*) DESC',('QUOTE',)).fetchall()
    st.dataframe([dict(stage=r[0],reason=r[1],count=r[2]) for r in rows],use_container_width=True)
dashboard()
periods={'1 час':1,'3 часа':3,'6 часов':6,'12 часов':12,'24 часа':24,'48 часов':48,'7 дней':168,'Весь эксперимент':None}
period=st.selectbox('Период протокола',list(periods),index=4)
st.caption('События и ряд equity — за выбранный период; Summary — текущее состояние и итог с начала эксперимента, не прибыль за период.')
if st.button('Подготовить Excel'):
    # Refresh in-memory read model from the worker before export.
    with db.lock:
        db.s=json.loads(db.cx.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        st.session_state['xlsx']=export_excel(db,periods[period])
        st.session_state['xlsx_period']=period
        st.session_state['xlsx_name']=f'V6_protocol_{periods[period] or "all"}h_{now_ms()}.xlsx'
if 'xlsx' in st.session_state and st.session_state.get('xlsx_period')==period:
    st.download_button('Скачать Excel',st.session_state['xlsx'],st.session_state['xlsx_name'],
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
