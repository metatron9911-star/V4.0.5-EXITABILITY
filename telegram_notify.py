import os
import requests
from typing import Any, Dict, List, Tuple

BASE = "https://api.telegram.org"


def _creds():
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()


def notify(text: str, return_detail: bool = False):
    token, chat = _creds()
    if not token or not chat:
        result = (False, "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
        return result if return_detail else False
    try:
        r = requests.post(
            f"{BASE}/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text}, timeout=8,
        )
        if r.ok:
            result = (True, "Telegram connection OK")
        else:
            try:
                desc = (r.json() or {}).get("description") or r.text[:200]
            except Exception:
                desc = r.text[:200]
            result = (False, f"Telegram API error {r.status_code}: {desc}")
        return result if return_detail else result[0]
    except Exception as exc:
        result = (False, f"Telegram request failed: {exc!r}")
        return result if return_detail else False


def get_me() -> Tuple[bool, str]:
    token, chat = _creds()
    if not token:
        return False, "bot token missing"
    try:
        r = requests.get(f"{BASE}/bot{token}/getMe", timeout=6)
        if not r.ok:
            return False, f"HTTP {r.status_code}"
        j = r.json() or {}
        username = ((j.get("result") or {}).get("username") or "bot")
        return True, f"@{username}; chat {'set' if chat else 'missing'}"
    except Exception as exc:
        return False, repr(exc)


def get_updates(offset: int = 0, timeout: int = 0) -> List[Dict[str, Any]]:
    token, _ = _creds()
    if not token:
        return []
    try:
        r = requests.get(
            f"{BASE}/bot{token}/getUpdates",
            params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'},
            timeout=max(6, timeout + 3),
        )
        if not r.ok:
            return []
        return (r.json() or {}).get("result") or []
    except Exception:
        return []


def test_message():
    mode = os.getenv("V4_MODE", "PAPER").strip() or "PAPER"
    return notify(
        "🚀 V4 Momentum Hunter v4.0.8\n"
        "Telegram connection OK ✅\n"
        f"Mode: {mode}\n"
        "AUTO trading: HARD LOCKED",
        return_detail=True,
    )
