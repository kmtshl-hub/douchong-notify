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
"""Polling fetch layer for Bilibili space dynamics (R-18 / T-602)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

import aiohttp

from .. import bilibili_gateway as gateway
from .. import runtime_state
from . import dedup, dynamic_notifier

LOGGER = logging.getLogger("bili_douchong.notify")

FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
ROOMS_PATH = Path(os.getenv("BILI_ROOMS_PATH", "/app/rooms.json"))
_FALSE_VALUES = {"0", "false", "no", "off"}
REQUEST_TIMEOUT_SECONDS = 15
UID_GAP_MIN_SECONDS = 1.0
UID_GAP_MAX_SECONDS = 2.0
DOWNLOAD_CONCURRENCY = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_DYNAMIC_IMAGES = 9
INITIAL_STATE_PREFIX = "qqnotify:dynamic-fetcher:initialized"

# -412 risk-control backoff: start at 5 minutes, double per hit, cap at 60 minutes.
BACKOFF_INITIAL_SECONDS = 5 * 60
BACKOFF_MULTIPLIER = 2
BACKOFF_MAX_SECONDS = 60 * 60

_backoff_seconds = BACKOFF_INITIAL_SECONDS
_backoff_until = 0.0


def _enabled(name: str, default: str = "1") -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        value = default
    return value.strip().lower() not in _FALSE_VALUES


def _poll_bounds() -> tuple[float, float]:
    def read(name: str, default: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default

    low = read("DYNAMIC_POLL_MIN_SECONDS", 60.0)
    high = read("DYNAMIC_POLL_MAX_SECONDS", 90.0)
    if high < low:
        low, high = high, low
    return low, high


def _poll_delay() -> float:
    low, high = _poll_bounds()
    return random.uniform(low, high)


def _uid_gap() -> float:
    return random.uniform(UID_GAP_MIN_SECONDS, UID_GAP_MAX_SECONDS)


def _backing_off(now: float | None = None) -> bool:
    return (time.monotonic() if now is None else now) < _backoff_until


def _trigger_backoff(now: float | None = None) -> float:
    global _backoff_seconds, _backoff_until
    current = time.monotonic() if now is None else now
    delay = _backoff_seconds
    _backoff_until = current + delay
    _backoff_seconds = min(
        BACKOFF_MAX_SECONDS,
        max(BACKOFF_INITIAL_SECONDS, delay * BACKOFF_MULTIPLIER),
    )
    return delay


def _reset_backoff() -> None:
    global _backoff_seconds, _backoff_until
    _backoff_seconds = BACKOFF_INITIAL_SECONDS
    _backoff_until = 0.0


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _extract_text_and_nodes(module_dynamic: object) -> tuple[str, list[dict[str, Any]]]:
    dynamic = _dict(module_dynamic)
    desc = _dict(dynamic.get("desc"))
    opus = _dict(_dict(dynamic.get("major")).get("opus"))
    summary = _dict(opus.get("summary"))

    # Both shapes coexist. Prefer the old desc when it actually contains text;
    # otherwise use itemOpusStyle summary. Nodes must come from the same source.
    source = desc if _str(desc.get("text")) else summary
    text = _str(source.get("text"))
    raw_nodes = source.get("rich_text_nodes")
    nodes: list[dict[str, Any]] = []
    if isinstance(raw_nodes, list):
        for raw in raw_nodes:
            node = _dict(raw)
            node_type = _str(node.get("type"))
            if node_type == "RICH_TEXT_NODE_TYPE_EMOJI":
                emoji = _dict(node.get("emoji"))
                url = _str(emoji.get("icon_url"))
                alt = _str(emoji.get("text")) or _str(node.get("text"))
                if alt.startswith("[") and alt.endswith("]"):
                    alt = alt[1:-1]
                nodes.append({"type": "emoji", "url": url, "alt": alt})
            else:
                # Unknown rich-node kinds retain their visible text in order.
                visible = _str(node.get("text"))
                if visible:
                    nodes.append({"type": "text", "text": visible})
    return text, nodes


def _extract_images(module_dynamic: object) -> list[str]:
    major = _dict(_dict(module_dynamic).get("major"))
    urls: list[str] = []
    for raw in _dict(major.get("draw")).get("items") or []:
        item = _dict(raw)
        url = _str(item.get("src")) or _str(item.get("url"))
        if url:
            urls.append(url)
    for raw in _dict(major.get("opus")).get("pics") or []:
        item = _dict(raw)
        url = _str(item.get("url")) or _str(item.get("src"))
        if url:
            urls.append(url)
    return urls[:MAX_DYNAMIC_IMAGES]


def _parse_item(item: object, uid: int) -> dict[str, Any]:
    raw = _dict(item)
    modules = _dict(raw.get("modules"))
    author = _dict(modules.get("module_author"))
    module_dynamic = _dict(modules.get("module_dynamic"))
    major = _dict(module_dynamic.get("major"))
    text, rich_nodes = _extract_text_and_nodes(module_dynamic)

    # `avatar` is a short non-URL value in the measured feed; `face` is the real URL.
    avatar_url = _str(author.get("face"))
    archive = _dict(major.get("archive"))
    video = None
    if archive:
        video = {
            "title": _str(archive.get("title")),
            "duration": _str(archive.get("duration_text")),
            "cover": None,
            "_cover_url": _str(archive.get("cover")),
        }

    forward = None
    if isinstance(raw.get("orig"), dict):
        parsed = _parse_item(raw["orig"], uid)
        forward = {
            "uname": parsed.get("uname"),
            "text": parsed.get("text"),
            "images": [],
            "rich_nodes": parsed.get("rich_nodes"),
            "_image_urls": parsed.get("_image_urls", []),
        }

    return {
        "dynamic_id": _str(raw.get("id_str")),
        "uid": uid,
        "uname": _str(author.get("name")),
        "avatar": None,
        "timestamp": author.get("pub_ts") if isinstance(author.get("pub_ts"), int) else None,
        "text": text,
        "images": [],
        "video": video,
        "forward": forward,
        "rich_nodes": rich_nodes if rich_nodes else None,
        "_avatar_url": avatar_url,
        "_image_urls": _extract_images(module_dynamic),
    }


async def fetch_once(uid: int) -> list[dict] | None:
    """Fetch one UID's latest dynamics and parse them; any failure returns None."""
    if not _enabled("NOTIFY_DYNAMIC_ENABLED", "0"):
        return None
    if _backing_off():
        return None
    try:
        gateway.init_session()
        await gateway.ensure_bili_ticket()
        session = runtime_state.aiohttp_session
        if session is None:
            LOGGER.warning("动态抓取共享 session 不可用 uid=%s", uid)
            return None
        params = {
            "host_mid": str(uid),
            "platform": "web",
            "offset": "",
            "timezone_offset": "-480",
            "features": "itemOpusStyle",
        }
        headers = {
            "User-Agent": gateway.USER_AGENT,
            "Referer": "https://space.bilibili.com/",
        }
        async with session.get(
            FEED_URL,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        ) as response:
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            LOGGER.warning("动态接口返回非对象 uid=%s", uid)
            return None
        code = payload.get("code")
        if code == -412:
            delay = _trigger_backoff()
            LOGGER.warning("动态接口触发 -412，退避 %.0f 秒 uid=%s", delay, uid)
            return None
        if code != 0:
            LOGGER.warning("动态接口失败 uid=%s code=%r", uid, code)
            return None
        _reset_backoff()
        items = _dict(payload.get("data")).get("items")
        if not isinstance(items, list):
            return []
        return [_parse_item(item, uid) for item in items if isinstance(item, dict)]
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK
        LOGGER.warning("动态抓取异常 uid=%s: %s", uid, type(exc).__name__)
        return None


async def _download_one(url: object, semaphore: asyncio.Semaphore) -> bytes | None:
    if not isinstance(url, str) or not url:
        return None
    try:
        session = runtime_state.aiohttp_session
        if session is None:
            return None
        headers = {
            "User-Agent": gateway.USER_AGENT,
            "Referer": "https://www.bilibili.com/",
        }
        async with semaphore:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as response:
                if response.status != 200:
                    return None
                declared = response.headers.get("Content-Length")
                if declared:
                    try:
                        if int(declared) > MAX_IMAGE_BYTES:
                            return None
                    except ValueError:
                        pass
                data = await response.read()
        if not data or len(data) > MAX_IMAGE_BYTES:
            return None
        return data
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK
        LOGGER.warning("动态素材下载失败: %s", type(exc).__name__)
        return None


async def _materialize(data: dict[str, Any]) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    avatar_task = asyncio.create_task(_download_one(data.get("_avatar_url"), semaphore))
    image_tasks = [
        asyncio.create_task(_download_one(url, semaphore))
        for url in (data.get("_image_urls") or [])[:MAX_DYNAMIC_IMAGES]
    ]
    video = data.get("video")
    cover_task = None
    if isinstance(video, dict):
        cover_task = asyncio.create_task(_download_one(video.get("_cover_url"), semaphore))
    forward = data.get("forward")
    forward_tasks: list[asyncio.Task[bytes | None]] = []
    if isinstance(forward, dict):
        forward_tasks = [
            asyncio.create_task(_download_one(url, semaphore))
            for url in (forward.get("_image_urls") or [])[:MAX_DYNAMIC_IMAGES]
        ]
    rich_tasks: list[tuple[dict[str, Any], asyncio.Task[bytes | None]]] = []
    rich_node_groups: list[object] = [data.get("rich_nodes")]
    if isinstance(forward, dict):
        rich_node_groups.append(forward.get("rich_nodes"))
    for nodes in rich_node_groups:
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if isinstance(node, dict) and node.get("type") == "emoji":
                rich_tasks.append(
                    (
                        node,
                        asyncio.create_task(
                            _download_one(node.get("url"), semaphore)
                        ),
                    )
                )

    data["avatar"] = await avatar_task
    data["images"] = [blob for blob in await asyncio.gather(*image_tasks) if blob]
    if isinstance(video, dict):
        video["cover"] = await cover_task if cover_task is not None else None
        video.pop("_cover_url", None)
    if isinstance(forward, dict):
        forward["images"] = [blob for blob in await asyncio.gather(*forward_tasks) if blob]
        forward.pop("_image_urls", None)
    for node, task in rich_tasks:
        node["image"] = await task
        node.pop("url", None)
    for nodes in rich_node_groups:
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if isinstance(node, dict):
                node.pop("url", None)

    data.pop("_avatar_url", None)
    data.pop("_image_urls", None)
    return data


def _initial_key(uid: int) -> str:
    return f"{INITIAL_STATE_PREFIX}:{uid}"


async def _read_cursor(uid: int) -> tuple[bool, str | None]:
    """Return (redis_ok, last_latest_id); a missing key means first observation."""
    try:
        client = await dedup._get_redis()  # reuse the project's one Redis client
        value = await client.get(_initial_key(uid))
        if value is None:
            return True, None
        return True, str(value)
    except Exception as exc:
        LOGGER.warning("动态首次静默状态读取失败 uid=%s: %s", uid, type(exc).__name__)
        return False, None


async def _write_cursor(uid: int, dynamic_id: str) -> bool:
    try:
        client = await dedup._get_redis()
        await client.set(_initial_key(uid), dynamic_id)
        return True
    except Exception as exc:
        LOGGER.warning("动态首次静默状态写入失败 uid=%s: %s", uid, type(exc).__name__)
        return False


async def _seed_latest(room_id: int, uid: int, items: list[dict[str, Any]]) -> bool:
    latest = next((item.get("dynamic_id") for item in items if item.get("dynamic_id")), None)
    if latest is None:
        return False
    try:
        await dedup.first_seen(
            dynamic_notifier.DYNAMIC_EVENT,
            room_id,
            latest,
            dynamic_notifier.DEDUP_TTL_SECONDS,
        )
    except Exception as exc:
        LOGGER.warning("动态首次静默去重预种失败 room=%s: %s", room_id, type(exc).__name__)
        return False
    return await _write_cursor(uid, str(latest))


def _new_prefix(items: list[dict[str, Any]], cursor: str | None) -> list[dict[str, Any]]:
    """Return feed items newer than the persisted latest id (feed is newest first)."""
    if cursor is None:
        return []
    fresh: list[dict[str, Any]] = []
    for item in items:
        dynamic_id = item.get("dynamic_id")
        if dynamic_id == cursor:
            break
        if dynamic_id:
            fresh.append(item)
    return fresh


def _load_room_ids() -> list[int]:
    try:
        payload = json.loads(ROOMS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("读取 rooms.json 失败: %s", type(exc).__name__)
        return []
    values = payload.get("room_ids") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return []
    result: list[int] = []
    for value in values:
        try:
            room_id = int(value)
        except (TypeError, ValueError):
            continue
        if room_id > 0:
            result.append(room_id)
    return result


async def _run_round() -> None:
    if not _enabled("NOTIFY_DYNAMIC_ENABLED", "0") or _backing_off():
        return
    rooms = _load_room_ids()
    for index, room_id in enumerate(rooms):
        if _backing_off():
            return  # -412 means no notifications at all for the rest of this round.
        try:
            init = await gateway.fetch_room_init(room_id)
            uid_value = _dict(init).get("uid")
            uid = int(uid_value) if uid_value is not None else 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("动态房间 UID 解析失败 room=%s: %s", room_id, type(exc).__name__)
            uid = 0
        if uid > 0:
            items = await fetch_once(uid)
            if items is not None:
                redis_ok, cursor = await _read_cursor(uid)
                if not redis_ok:
                    items = []  # Redis unknown: fail closed against replaying old dynamics.
                elif cursor is None:
                    if _enabled("BILI_NOTIFY_INITIAL_SILENT"):
                        await _seed_latest(room_id, uid, items)
                        items = []
                    else:
                        # Explicitly disabling initial silence means the current feed may notify.
                        # Persist newest now so later rounds only inspect the new prefix.
                        latest = next((item.get("dynamic_id") for item in items if item.get("dynamic_id")), None)
                        if latest is not None:
                            await _write_cursor(uid, str(latest))
                else:
                    items = _new_prefix(items, cursor)
                    latest = next((item.get("dynamic_id") for item in items if item.get("dynamic_id")), None)
                    if latest is not None:
                        await _write_cursor(uid, str(latest))
                for item in reversed(items):  # oldest first when several arrived between polls
                    if _backing_off():
                        return
                    try:
                        await dynamic_notifier.notify_dynamic(
                            room_id,
                            data=await _materialize(item),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # notifier is defensive too; keep fetch loop immortal.
                        LOGGER.warning("动态通知调用异常 room=%s: %s", room_id, type(exc).__name__)
        if index + 1 < len(rooms):
            await asyncio.sleep(_uid_gap())


async def run_forever() -> None:
    """Main loop: poll all monitored rooms forever unless the task is cancelled."""
    while True:
        try:
            if _enabled("NOTIFY_DYNAMIC_ENABLED", "0"):
                await _run_round()
            delay = _poll_delay()
            if _backing_off():
                delay = max(delay, _backoff_until - time.monotonic())
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK
            LOGGER.warning("动态抓取主循环异常: %s", type(exc).__name__)
            await asyncio.sleep(_poll_delay())
