"""Video search with a bounded TTL cache in front of the Google client."""

from __future__ import annotations

from ..cache import TTLCache
from ..schemas import SearchVideosRequest, SearchVideosResult
from ..search_client import YouTubeSearchClient


class SearchService:
    def __init__(
        self,
        *,
        client: YouTubeSearchClient,
        cache: TTLCache[SearchVideosResult],
        ttl_seconds: float,
    ) -> None:
        self._client = client
        self._cache = cache
        self._ttl = ttl_seconds

    @property
    def configured(self) -> bool:
        return self._client.configured

    async def search(self, request: SearchVideosRequest) -> SearchVideosResult:
        key = "search:" + request.cache_key()
        cached = self._cache.get(key)
        if cached is not None:
            return cached.model_copy(update={"cache_hit": True})
        result = await self._client.search(request)
        size = len(result.model_dump_json().encode("utf-8"))
        self._cache.set(key, result, ttl_seconds=self._ttl, size_bytes=size)
        return result
