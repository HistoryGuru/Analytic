"""
Lightweight in-memory TTL cache.

We deliberately do NOT build a persistent database (per the project's design:
every lookup is a live, on-demand scrape/query). This cache just avoids
hammering Tabroom/Opencaselist/Tournaments.Tech with duplicate requests for
the same debater within a short window, and speeds up repeat searches.

Because it's in-memory, it resets on every deploy/restart on Render -- that's
intentional and fine for this use case. If this ever needs to survive
restarts or be shared across multiple Render instances, swap this out for a
Redis-backed cache (see README).
"""
from cachetools import TTLCache

from app.config import get_settings

_settings = get_settings()

# maxsize is generous since values are small JSON-ish dicts, not files.
lookup_cache: TTLCache = TTLCache(maxsize=512, ttl=_settings.cache_ttl_seconds)


def cache_key(*parts: str) -> str:
    return "|".join(p.strip().lower() for p in parts if p)
