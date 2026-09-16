"""Explicit button-triggered Telegram messages; no trades or secret logging."""
import os
from engine import now_ms

def configured():
    return bool(os.getenv('TELEGRAM_BOT_TOKEN','').strip() and os.getenv('TELEGRAM_CHAT_ID','').strip())

def status_text(db,s):
    def money(x):return 'UNKNOWN' if x is None else f'${x:.2f}'
    lines=['V6 — PAPER status','Real execution: DISABLED',
        'Experiment: '+s['config']['experiment'],
        'New entries: '+('PAUSED' if s['paused'] else 'ENABLED')]
    for r in db.stats(s):
        lines.extend([r['policy'],f"Cash {money(r['cash'])} | Equity {money(r['equity'])}",
            f"Net PNL {money(r['net_pnl'])} | Open {r['open']} | Closed {r['closed']} | Unknown {r['unknown']}"])
    for name in ('scanner','exits'):
        h=s['heartbeat'].get(name)
        lines.append(f"{name}: "+(f"{h['status']}, {(now_ms()-h['ts_ms'])//1000}s ago" if h else 'NO HEARTBEAT'))
    lines.append('Two alternative virtual accounts; do not sum balances.')
    return '\n'.join(lines)

def send(text, post=None):
    if not configured():return False,'Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID'
    if post is None:
        import requests
        post=requests.post
    try:
        r=post('https://api.telegram.org/bot'+os.environ['TELEGRAM_BOT_TOKEN'].strip()+'/sendMessage',
            json={'chat_id':os.environ['TELEGRAM_CHAT_ID'].strip(),'text':text},timeout=8)
        if r.ok and r.json().get('ok') is True:return True,'Telegram подтвердил отправку'
        return False,f'Telegram отклонил запрос: HTTP {r.status_code}'
    except Exception as exc:
        return False,'Отправка не подтверждена: '+type(exc).__name__+'; проверь чат перед повтором'
