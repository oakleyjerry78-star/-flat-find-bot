# state.py
from typing import Dict, List, Optional
from telebot import TeleBot

# ==== ГЛОБАЛЬНИЙ СТАН (спільний для всіх модулів) ====
page_msg_ids: Dict[int, Dict[int, List[int]]] = {}      # chat_id -> page -> [message_ids]
loading_notice_msg_id: Dict[int, int] = {}
next_prompt_msg_id: Dict[int, int] = {}

user_listings: Dict[int, List[dict]] = {}
user_page: Dict[int, int] = {}
user_loading_status: Dict[int, bool] = {}
user_last_queries: Dict[int, dict] = {}
current_category: Dict[int, str] = {}
active_search_token: Dict[int, str] = {}



# Пороги для показу підказки "Далі"
BIG_RESULTS_TOTAL_THRESHOLD = 50
BIG_RESULTS_NEW_THRESHOLD = 20

# ==== ХЕЛПЕРИ ДЛЯ БЕЗПЕЧНОГО ВИДАЛЕННЯ / ОЧИСТКИ UI ====
def safe_delete_message(bot: TeleBot, chat_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass

def safe_send_message(bot: TeleBot, chat_id: int, text: str, **kw):
    try:
        return bot.send_message(chat_id, text, **kw)
    except Exception:
        return None

def _clear_ui(bot: TeleBot, chat_id: int) -> None:
    # Прибрати усі картки поточної сторінки/сторінок
    for msgs in (page_msg_ids.get(chat_id, {}) or {}).values():
        for mid in msgs or []:
            safe_delete_message(bot, chat_id, mid)
    page_msg_ids[chat_id] = {}

    # Прибрати службові нотифікації
    for holder in (loading_notice_msg_id, next_prompt_msg_id):
        mid = holder.pop(chat_id, None)
        if mid:
            safe_delete_message(bot, chat_id, mid)

def begin_category_session(bot: TeleBot, chat_id: int, category: str) -> str:
    """Старт нової сесії пошуку для категорії (квартира/будинок/кімната/офіс)."""
    import uuid
    _clear_ui(bot, chat_id)

    user_listings[chat_id] = []
    user_page[chat_id] = 0
    user_loading_status[chat_id] = False
    user_last_queries.pop(chat_id, None)
    current_category[chat_id] = category

    token = uuid.uuid4().hex
    active_search_token[chat_id] = token
    return token
