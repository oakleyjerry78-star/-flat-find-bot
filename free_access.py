from __future__ import annotations

from typing import Any

from gsheets import get_free_view_usage, get_sub_info, register_free_listing_view

FREE_MONTHLY_LISTINGS = 3


def has_active_subscription(user_id: int | str) -> bool:
    try:
        return str(get_sub_info(str(user_id)) or "").strip().upper() in {"TRUE", "1", "YES", "Y", "T"}
    except Exception as e:
        print("[subscription check error]", e)
        return False


def listing_key(card: dict[str, Any]) -> str:
    return str(
        card.get("_key")
        or card.get("link")
        or f"{card.get('title', '')}|{card.get('price', '')}"
        or ""
    ).strip()


def free_views_used_up(user_id: int | str) -> bool:
    if has_active_subscription(user_id):
        return False
    usage = get_free_view_usage(str(user_id), monthly_limit=FREE_MONTHLY_LISTINGS)
    return int(usage.get("remaining", 0) or 0) <= 0


def rent_access_blocked(user_id: int | str) -> bool:
    return (not has_active_subscription(user_id)) and free_views_used_up(user_id)


def register_listing_view(user_id: int | str, card: dict[str, Any]) -> dict[str, Any]:
    if has_active_subscription(user_id):
        return {
            "allowed": True,
            "used": 0,
            "remaining": FREE_MONTHLY_LISTINGS,
            "already_seen": False,
            "subscribed": True,
        }
    result = register_free_listing_view(
        str(user_id),
        listing_key(card),
        monthly_limit=FREE_MONTHLY_LISTINGS,
    )
    result["subscribed"] = False
    return result
