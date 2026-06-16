# providers/olx_provider.py

# --- Apartments ---
from .olx import get_olx_provider as _factory_get

# --- Houses (підтримуємо обидві назви класу) ---
try:
    from .olx.olx_house import OlxProviderHouses as OlxProviderHouse
except ImportError:
    from .olx.olx_house import OlxProviderHouse as OlxProviderHouse

# --- Rooms ---
from .olx.olx_rooms import OlxProviderRooms

# --- Offices (обидві назви класу) ---
try:
    from .olx.olx_office import OlxProviderOffices as OlxProviderOffice
except ImportError:
    from .olx.olx_office import OlxProviderOffice as OlxProviderOffice


def get_olx_provider(category: str, user_agent=None, proxy=None, always_no_fee=True):
    """
    Новий універсальний вхід. Створює провайдер за категорією.
    """
    return _factory_get(
        category,
        user_agent=user_agent,
        proxy=proxy,
        always_no_fee=always_no_fee,
    )

def create(category: str, user_agent=None, proxy=None):
    """
    LEGACY-аліас для старого коду (наприклад, providers/aggregate.py).
    Поведінка як раніше: приймає лише user_agent і proxy.
    """
    return _factory_get(category, user_agent=user_agent, proxy=proxy)