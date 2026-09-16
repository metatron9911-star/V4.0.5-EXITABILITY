import json
import streamlit as st
from engine import Store, export_excel, now_ms

st.set_page_config(page_title='V6 PAPER Experiment',layout='wide')
st.title('V6 — PAPER experiment')
st.info('Реальные сделки отключены в коде. Два отдельных виртуальных счёта: их балансы не складываются.')
@st.cache_resource
def connect(): return Store()
try: db=connect()
except Exception as exc:
    st.error(str(exc)); st.stop()

@st.fragment(run_every='10s')
def dashboard():
    state=db.cx.execute('SELECT payload FROM state WHERE id=1').fetchone()
    s=json.loads(state[0])
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
if st.button('Подготовить Excel — весь эксперимент'):
    # Refresh in-memory read model from the worker before export.
    with db.lock:
        db.s=json.loads(db.cx.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        st.session_state['xlsx']=export_excel(db)
if 'xlsx' in st.session_state:
    st.download_button('Скачать Excel',st.session_state['xlsx'],'V6_protocol.xlsx',
        mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
