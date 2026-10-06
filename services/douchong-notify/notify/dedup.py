# Copyright (C) 2026 小恩 <https://github.com/kmtshl-hub>
#
# This file is part of the douchong-notify extension for VR_douchong
#   (https://github.com/QianQiuZy/VR_douchong).
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License only.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see
# <https://www.gnu.org/licenses/old-licenses/gpl-2.0.html>.
#
"""Async Redis-backed notification deduplication."""

from __future__ import annotations

import logging
import os
from typing import Any

from . import sub_client

LOGGER = logging.getLogger("bili_douchong.notify")

DEDUP_PREFIX = "qqnotify:dedup"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_redis_client: Any | None = None


def dedup_key(event: str, room_id: int | str, event_id: str | int) -> str:
    """Forward to the single shared key builder in ``sub_client``."""
    return sub_client.dedup_key(event, room_id, event_id)


async def _get_redis() -> Any:
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as redis_asyncio

        _redis_client = redis_asyncio.Redis.from_url(
            REDIS_URL, decode_responses=True
        )
    return _redis_client


async def first_seen(
    event: str,
    room_id: int | str,
    event_id: str | int,
    ttl_seconds: int,
) -> bool:
    """Return ``True`` only for a newly inserted key; Redis failure fails open."""
    key = dedup_key(event, room_id, event_id)
    try:
        client = await _get_redis()
        result = await client.set(key, 1, nx=True, ex=ttl_seconds)
        return bool(result)
    except Exception as exc:
        LOGGER.warning(
            "Redis 去重不可用，fail open 允许发送: %s", type(exc).__name__
        )
        return True


async def close() -> None:
    """Close the lazily-created async Redis client, if any."""
    global _redis_client
    client = _redis_client
    _redis_client = None
    if client is not None:
        await client.aclose()
