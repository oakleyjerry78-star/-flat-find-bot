# providers/olx/__init__.py
"""
OLX providers registry + factory.
Підтримувані категорії: apartment, house, room, office
Працює навіть якщо класи називаються по-різному (Rooms/House/…).
"""
import inspect
from importlib import import_module
from inspect import isclass
from typing import Optional, Type

try:
    from providers.base import Provider
  # базовий клас провайдерів
except Exception:
    Provider = object  # запасний варіант, щоб не впасти на імпорт

def _load_provider(module_name: str, candidates: list[str]) -> Optional[Type]:
    """
    Повертає клас провайдера з модуля:
    1) пробує список імен-кандидатів;
    2) якщо не знайшло — шукає будь-який клас, що наслідує Provider,
       і має метод build_url або атрибут CATEGORY_PATH.
    """
    try:
        m = import_module(module_name)
    except Exception as e:
        print(f"[OLX INIT] import error for {module_name}: {e}")
        return None

    # 1) Явні кандидати
    for name in candidates:
        if hasattr(m, name):
            cls = getattr(m, name)
            if isclass(cls):
                return cls

    # 2) Автопошук класу-провайдера
    best = None
    for attr in dir(m):
        obj = getattr(m, attr)
        if isclass(obj):
            # якщо є Provider — орієнтуємось на нього
            if Provider is not object and issubclass(obj, Provider) and obj is not Provider:
                best = obj
                break
            # fallback: клас з build_url
            if hasattr(obj, "build_url"):
                best = obj
    return best

# Мапа модуль -> можливі імена класів
MODULES = {
    "apartment": ("providers.olx.olx_apartments",
                  ["OlxProvider", "OlxProviderApartments", "OlxProviderApartment", "ApartmentsProvider"]),
    "house":     ("providers.olx.olx_house",
                  ["OlxProvider", "OlxProviderHouse", "OlxProviderHouses", "HouseProvider"]),
    "apartment_buy": ("providers.olx.olx_apartments",
                  ["OlxProvider", "OlxProviderApartments", "OlxProviderApartment", "ApartmentsProvider"]),
    "house_buy": ("providers.olx.olx_house",
                  ["OlxProvider", "OlxProviderHouse", "OlxProviderHouses", "HouseProvider"]),
    "room":      ("providers.olx.olx_rooms",
                  ["OlxProvider", "OlxProviderRooms", "OlxProviderRoom", "RoomsProvider"]),
    "office":    ("providers.olx.olx_office",
                  ["OlxProvider", "OlxProviderOffice", "OfficeProvider"]),
}

OlxProviderApartments = _load_provider(*MODULES["apartment"])
OlxProviderHouse      = _load_provider(*MODULES["house"])
OlxProviderRooms      = _load_provider(*MODULES["room"])
OlxProviderOffice     = _load_provider(*MODULES["office"])

CATEGORY_TO_PROVIDER = {}
if OlxProviderApartments: CATEGORY_TO_PROVIDER["apartment"] = OlxProviderApartments
if OlxProviderHouse:      CATEGORY_TO_PROVIDER["house"]     = OlxProviderHouse
if OlxProviderApartments: CATEGORY_TO_PROVIDER["apartment_buy"] = OlxProviderApartments
if OlxProviderHouse:      CATEGORY_TO_PROVIDER["house_buy"]     = OlxProviderHouse
if OlxProviderRooms:      CATEGORY_TO_PROVIDER["room"]      = OlxProviderRooms
if OlxProviderOffice:     CATEGORY_TO_PROVIDER["office"]    = OlxProviderOffice

__all__ = [
    "OlxProviderApartments",
    "OlxProviderHouse",
    "OlxProviderRooms",
    "OlxProviderOffice",
    "get_olx_provider",
]

def get_olx_provider(category: str, **kwargs):
    c = (category or "apartment").strip().lower()
    ProviderClass = CATEGORY_TO_PROVIDER.get(c) or CATEGORY_TO_PROVIDER.get("apartment") or CATEGORY_TO_PROVIDER.get("office")
    if ProviderClass is None:
        raise RuntimeError("Жоден OLX провайдер не імпортувався успішно")

    sig = inspect.signature(ProviderClass.__init__)
    ctor_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

    inst = ProviderClass(**ctor_kwargs)

    # Якщо фабрика отримала always_no_fee, але конструктор його не підтримує —
    # тихо виставимо атрибут на інстансі.
    if "always_no_fee" in kwargs and "always_no_fee" not in sig.parameters:
        try:
            setattr(inst, "always_no_fee", bool(kwargs["always_no_fee"]))
        except Exception:
            pass

    return inst
