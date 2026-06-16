# utils/messaging.py
import time
import threading
from telebot.apihelper import ApiTelegramException
import requests

_BLOCKED_USERS = set()
_LOCK = threading.Lock()

def safe_send(chat_id, send_fn, *args, **kwargs):
    """
    Викликає bot.send_* з ретраями та фільтрує 403 (user blocked),
    429 (rate limit) і тимчасові мережеві збої.
    Використання: safe_send(chat_id, bot.send_message, "текст")
    """
    with _LOCK:
        if chat_id in _BLOCKED_USERS:
            return False

    for attempt in range(3):
        try:
            return send_fn(chat_id, *args, **kwargs)

        except ApiTelegramException as e:
            desc = getattr(e, "description", str(e))
            code = getattr(e, "error_code", None)

            # користувач заблокував бота / чат видалено / немає доступу
            if code == 403 or "blocked by the user" in desc.lower() or "chat not found" in desc.lower():
                with _LOCK:
                    _BLOCKED_USERS.add(chat_id)
                print(f"[SKIP] 403/blocked: {chat_id}")
                return False

            # ліміт: почекай і повтори
            if code == 429:
                retry_after = (getattr(e, "result_json", {}) or {}).get("parameters", {}).get("retry_after", 1)
                time.sleep(float(retry_after) + 1)
                continue

            # інші 4xx — не ретраїмо
            if code and 400 <= code < 500:
                print(f"[SKIP] {code}: {desc}")
                return False

            # інші помилки Telegram — пробуємо з бекофом
            time.sleep(2 ** attempt)

        except requests.exceptions.RequestException as e:
            # мережеві збої — exponential backoff
            print(f"[WARN] network error: {e} (attempt {attempt+1}/3)")
            time.sleep(2 ** attempt)

    return False
