"""Video search and metadata retrieval with a bounded TTL cache in front of the Google client."""

from __future__ import annotations

from typing import Any

from ..cache import TTLCache
from ..errors import ErrorCode, TubeTraceError
from ..schemas import (
    ChannelPlaylistsResult,
    ChannelResult,
    GetChannelRequest,
    GetPlaylistRequest,
    GetVideoRequest,
    ListChannelPlaylistsRequest,
    PlaylistResult,
    PlaylistSummary,
    SearchVideosRequest,
    SearchVideosResult,
    VideoMetadataResult,
)
from ..search_client import PlaylistItemsPage, YouTubeSearchClient
from ..video_input import (
    ChannelTarget,
    parse_channel_input,
    parse_playlist_input,
    parse_video_with_playlist,
)


class SearchService:
    def __init__(
        self,
        *,
        client: YouTubeSearchClient,
        cache: TTLCache[Any],
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
        if isinstance(cached, SearchVideosResult):
            return cached.model_copy(update={"cache_hit": True})
        result = await self._client.search(request)
        size = len(result.model_dump_json().encode("utf-8"))
        self._cache.set(key, result, ttl_seconds=self._ttl, size_bytes=size)
        return result

    async def get_video(self, request: GetVideoRequest) -> VideoMetadataResult:
        parsed = parse_video_with_playlist(request.video)
        explicit_playlist = request.playlist is not None
        playlist_id = (
            parse_playlist_input(request.playlist)
            if request.playlist is not None
            else parsed.playlist_id
        )

        video_key = f"video:{parsed.video_id}"
        cached_video = self._cache.get(video_key)
        if isinstance(cached_video, VideoMetadataResult):
            video = cached_video
            video_hit = True
        else:
            video = await self._client.get_video(parsed.video_id)
            size = len(video.model_dump_json().encode("utf-8"))
            self._cache.set(video_key, video, ttl_seconds=self._ttl, size_bytes=size)
            video_hit = False

        if playlist_id is None:
            return video.model_copy(update={"cache_hit": video_hit})

        try:
            playlist_meta, playlist_hit = await self._get_playlist_meta(playlist_id)
        except TubeTraceError as err:
            if not explicit_playlist and err.code == ErrorCode.PLAYLIST_NOT_FOUND:
                return video.model_copy(update={"cache_hit": video_hit})
            raise

        summary = PlaylistSummary(
            playlist_id=playlist_meta.playlist_id,
            playlist_url=playlist_meta.playlist_url,
            title=playlist_meta.title,
            channel_id=playlist_meta.channel_id,
            channel_title=playlist_meta.channel_title,
            item_count=playlist_meta.item_count,
        )
        return video.model_copy(
            update={"playlist": summary, "cache_hit": video_hit and playlist_hit}
        )

    async def get_channel(self, request: GetChannelRequest) -> ChannelResult:
        target = parse_channel_input(request.channel)
        channel_meta, meta_hit = await self._get_channel_meta(target)
        if not request.include_videos:
            return channel_meta.model_copy(update={"videos": None, "cache_hit": meta_hit})
        if not channel_meta.uploads_playlist_id:
            return channel_meta.model_copy(update={"videos": [], "cache_hit": meta_hit})

        page, items_hit = await self._get_playlist_items(
            channel_meta.uploads_playlist_id,
            max_results=request.max_results,
            page_token=request.page_token,
        )
        return channel_meta.model_copy(
            update={
                "videos": page.items,
                "next_page_token": page.next_page_token,
                "prev_page_token": page.prev_page_token,
                "retrieved_at": page.retrieved_at,
                "cache_hit": meta_hit and items_hit,
            }
        )

    async def get_playlist(self, request: GetPlaylistRequest) -> PlaylistResult:
        playlist_id = parse_playlist_input(request.playlist)
        playlist_meta, meta_hit = await self._get_playlist_meta(playlist_id)
        if not request.include_videos:
            return playlist_meta.model_copy(update={"items": None, "cache_hit": meta_hit})

        page, items_hit = await self._get_playlist_items(
            playlist_meta.playlist_id,
            max_results=request.max_results,
            page_token=request.page_token,
        )
        return playlist_meta.model_copy(
            update={
                "items": page.items,
                "next_page_token": page.next_page_token,
                "prev_page_token": page.prev_page_token,
                "retrieved_at": page.retrieved_at,
                "cache_hit": meta_hit and items_hit,
            }
        )

    async def list_channel_playlists(
        self, request: ListChannelPlaylistsRequest
    ) -> ChannelPlaylistsResult:
        target = parse_channel_input(request.channel)
        if target.channel_id is not None:
            channel_id = target.channel_id
            meta_hit = True
        else:
            channel_meta, meta_hit = await self._get_channel_meta(target)
            channel_id = channel_meta.channel_id

        key = f"channel_playlists:{channel_id}:{request.max_results}:{request.page_token or ''}"
        cached = self._cache.get(key)
        if isinstance(cached, ChannelPlaylistsResult):
            return cached.model_copy(update={"cache_hit": meta_hit})
        result = await self._client.list_channel_playlists(
            channel_id, max_results=request.max_results, page_token=request.page_token
        )
        size = len(result.model_dump_json().encode("utf-8"))
        self._cache.set(key, result, ttl_seconds=self._ttl, size_bytes=size)
        return result

    async def _get_channel_meta(self, target: ChannelTarget) -> tuple[ChannelResult, bool]:
        key = f"channel_meta:{target.cache_token}"
        cached = self._cache.get(key)
        if isinstance(cached, ChannelResult):
            return cached, True
        channel = await self._client.get_channel(target)
        size = len(channel.model_dump_json().encode("utf-8"))
        self._cache.set(key, channel, ttl_seconds=self._ttl, size_bytes=size)
        if channel.channel_id:
            self._cache.set(
                f"channel_meta:id:{channel.channel_id}",
                channel,
                ttl_seconds=self._ttl,
                size_bytes=size,
            )
        return channel, False

    async def _get_playlist_meta(self, playlist_id: str) -> tuple[PlaylistResult, bool]:
        key = f"playlist_meta:{playlist_id}"
        cached = self._cache.get(key)
        if isinstance(cached, PlaylistResult):
            return cached, True
        playlist = await self._client.get_playlist(playlist_id)
        size = len(playlist.model_dump_json().encode("utf-8"))
        self._cache.set(key, playlist, ttl_seconds=self._ttl, size_bytes=size)
        return playlist, False

    async def _get_playlist_items(
        self,
        playlist_id: str,
        *,
        max_results: int,
        page_token: str | None,
    ) -> tuple[PlaylistItemsPage, bool]:
        key = f"playlist_items:{playlist_id}:{max_results}:{page_token or ''}"
        cached = self._cache.get(key)
        if isinstance(cached, PlaylistItemsPage):
            return cached, True
        page = await self._client.list_playlist_items(
            playlist_id, max_results=max_results, page_token=page_token
        )
        size = 256 + sum(len(item.model_dump_json().encode("utf-8")) for item in page.items)
        self._cache.set(key, page, ttl_seconds=self._ttl, size_bytes=size)
        return page, False
