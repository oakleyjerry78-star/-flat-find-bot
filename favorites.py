from __future__ import annotations

import hashlib
from typing import Any


_CARD_REGISTRY: dict[str, dict[str, Any]] = {}


def card_identity(card: dict[str, Any]) -> str:
    return str(
        card.get("_key")
        or card.get("link")
        or f"{card.get('title', '')}|{card.get('price', '')}"
        or ""
    ).strip()


def remember_card(card: dict[str, Any]) -> str:
    identity = card_identity(card)
    token = hashlib.md5(identity.encode("utf-8")).hexdigest()[:16] if identity else hashlib.md5(repr(card).encode("utf-8")).hexdigest()[:16]
    _CARD_REGISTRY[token] = dict(card)
    return token


def get_remembered_card(token: str) -> dict[str, Any] | None:
    return _CARD_REGISTRY.get(str(token or "").strip())
