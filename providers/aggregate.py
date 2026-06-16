# providers/aggregate.py
from typing import Dict, Any, List
from .base import Listing, Provider
from .olx_provider import get_olx_provider



class Aggregator:
    def __init__(self, providers: List[Provider], verbose: bool = False):
        self.providers = providers
        self.verbose = verbose

    def search(self, query: Dict[str, Any], limit: int = 30) -> List[Listing]:
        all_items: List[Listing] = []

        for provider in self.providers:
            try:
                if self.verbose:
                    print(f"[AGG] → {provider.source}:{getattr(provider, 'kind', '?')} | q={query}")
                    if hasattr(provider, "build_url"):
                        try:
                            url = provider.build_url(query, page=1)  # може повернути None
                            if url:
                                print(f"[AGG][{provider.source}:{getattr(provider, 'kind', '?')}] [URL][p1]: {url}")
                        except Exception as e:
                            print(f"[AGG] build_url warn {provider.source}:{getattr(provider, 'kind', '?')} → {e}")

                part = provider.search(query)
                if self.verbose:
                    print(f"[AGG] ← {provider.source}:{getattr(provider, 'kind', '?')} | items={len(part)}")
                all_items.extend(part)

            except Exception as e:
                print(f"[AGG][ERROR] {provider.source}:{getattr(provider, 'kind', '?')} → {e}")
                continue

        # Дедуп на рівні агрегатора
        uniq, seen = [], set()
        for it in all_items:
            key = (it.source, it.id or it.url)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)

        # Сортування: спочатку з ціною (зростання), потім без ціни
        uniq.sort(key=lambda x: (x.price_uah is None, x.price_uah or 0))
        return uniq[:limit]


def build_default_aggregator(kind: str = "apartment", verbose: bool = False) -> Aggregator:
    """
    Швидка збірка OLX + DOM.RIA під один тип ('apartment'/'house'/'room'/'office')
    """
    kind = (kind or "apartment").lower()
    providers: List[Provider] = []

    # OLX
    try:
        providers.append(get_olx_provider(kind))
    except Exception as e:
        print(f"[AGG] skip OLX {kind}: {e}")



    return Aggregator(providers, verbose=verbose)
