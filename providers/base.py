# providers/base.py
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from abc import ABC, abstractmethod

@dataclass
class Listing:
    id: str
    source: str
    url: str
    title: str
    price_uah: Optional[int] = None
    currency: Optional[str] = "UAH"
    city: Optional[str] = None
    district: Optional[str] = None
    address: Optional[str] = None
    rooms: Optional[int] = None
    area_total: Optional[float] = None
    floor: Optional[int] = None
    floors_total: Optional[int] = None
    is_no_fee: Optional[bool] = None
    allows_pets: Optional[bool] = None
    description: Optional[str] = None
    photos: List[str] = field(default_factory=list)
    author: Optional[str] = None
    posted_at: Optional[str] = None
    scraped_at: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class Provider(ABC):
    source: str = "base"
    kind: str = "generic"  # зручно для логів: "apartment"/"house"/"room"/"office"

    @abstractmethod
    def search(self, query: Dict[str, Any]) -> List[Listing]:
        """Sync-пошук. query містить усі фільтри з бота."""
        raise NotImplementedError

    # не обов’язково, але зручно для дебагу/тестів
    def build_url(self, query: Dict[str, Any], page: int = 1) -> Optional[str]:
        return None

    # хелпер, якщо хочеш централізовано фан-аут по районах
    def split_by_districts(self, query: Dict[str, Any]) -> List[Dict[str, Any]]:
        districts = (query or {}).get("districts") or []
        if len(districts) <= 1:
            return [query]
        return [{**query, "districts": [d]} for d in districts]
