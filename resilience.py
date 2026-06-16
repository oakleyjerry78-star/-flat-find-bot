# resilience.py
import traceback
from telebot.apihelper import ApiTelegramException

ADMIN_CHAT_ID = 601366483  # за бажанням: свій chat_id для тихих алертів

def safe_handler(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ApiTelegramException as e:
            desc = getattr(e, 'result_json', {}).get('description', str(e)).lower()
            benign = (
                'message is not modified',
                'bot was blocked by the user',
                'message to edit not found'
            )
            if any(b in desc for b in benign):
                return
            print(f"[TG API ERROR] {desc}")
        except Exception as e:
            print(f"[HANDLER ERROR] {func.__name__}: {e}\n{traceback.format_exc()}")
            try:
                if ADMIN_CHAT_ID and hasattr(args[0], "chat") and hasattr(args[0].chat, "id"):
                    # якщо це message/call — тихий алерт (не обов’язково)
                    args[0].bot.send_message(ADMIN_CHAT_ID, f"⚠️ Handler {func.__name__} error: {e}")
            except Exception:
                pass
    return wrapper
