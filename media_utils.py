from __future__ import annotations

from pathlib import Path

from telebot import types


MEDIA_DIR = Path(__file__).resolve().parent / "media"


def _path(name: str) -> Path:
    return MEDIA_DIR / name


def send_step_photo(bot, chat_id: int, image_name: str, caption: str, **kwargs):
    with _path(image_name).open("rb") as photo:
        return bot.send_photo(chat_id, photo, caption=caption, **kwargs)


def edit_step_photo(bot, chat_id: int, message_id: int, image_name: str, caption: str, **kwargs):
    reply_markup = kwargs.pop("reply_markup", None)
    parse_mode = kwargs.pop("parse_mode", None)
    with _path(image_name).open("rb") as photo:
        media = types.InputMediaPhoto(photo, caption=caption, parse_mode=parse_mode)
        return bot.edit_message_media(
            media=media,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
