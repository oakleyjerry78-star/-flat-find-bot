# helpers.py
from telebot.apihelper import ApiTelegramException

def safe_send(bot, method, *args, **kwargs):
    try:
        return getattr(bot, method)(*args, **kwargs)
    except ApiTelegramException as e:
        desc = getattr(e, 'result_json', {}).get('description', str(e)).lower()
        benign = (
            'message is not modified',
            'bot was blocked by the user',
            'message to edit not found'
        )
        if any(b in desc for b in benign):
            return None
        print(f"[SEND ERROR] {method}: {desc}")
    except Exception as e:
        print(f"[SEND ERROR] {method}: {e}")
    return None
