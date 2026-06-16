def safe_scroll(page, amount: int = 1200) -> bool:
    """Scroll without crashing the whole search if Playwright target closes."""
    try:
        page.mouse.wheel(0, amount)
        return True
    except Exception:
        try:
            page.evaluate("(y) => window.scrollBy(0, y)", amount)
            return True
        except Exception:
            return False
