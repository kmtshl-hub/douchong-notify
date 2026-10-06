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

"""Read-only client for qqbot-core subscription snapshots."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

LOGGER = logging.getLogger("bili_douchong.notify")

NOTIFY_API_URL = os.getenv(
    "QQBOT_NOTIFY_URL", "http://bili-douchong-qqbot-core:14667"
).rstrip("/")
NOTIFY_API_TOKEN = os.getenv("NOTIFY_API_TOKEN", "")
SUBSCRIPTIONS_PATH = "/subscriptions"
CACHE_TTL_SECONDS = 30
STALE_LIMIT_SECONDS = 300
EXPECTED_VERSION = 2
SNAPSHOT_EVENTS = ("live", "liveEnd", "dynamic", "sc")

_session: Any | None = None
_cache_snapshot: dict | None = None
_cache_success_at: float | None = None
_last_failure_reason: str | None = None


def parse_snapshot(payload: object) -> dict | None:
    """Return a usable v2 snapshot, or ``None`` for an incompatible payload."""
    if not isinstance(payload, dict):
        return None
    if type(payload.get("version")) is not int:
        return None
    if payload.get("version") != EXPECTED_VERSION:
        return None
    if not isinstance(payload.get("groups"), dict):
        return None
    return payload


def select_targets_detailed(
    snapshot: dict, room_id: int, event: str
) -> list[dict]:
    """Select target groups and per-group flags for ``room_id``/``event``."""
    if event not in SNAPSHOT_EVENTS:
        return []

    groups = snapshot.get("groups")
    if not isinstance(groups, dict):
        return []

    selected: dict[int, bool] = {}
    room_key = str(room_id)

    for group_id, group in groups.items():
        if not isinstance(group, dict):
            continue
        anchors = group.get("anchors", {})
        if not isinstance(anchors, dict):
            continue
        anchor = anchors.get(room_key)
        if not isinstance(anchor, dict):
            continue
        notify = anchor.get("notify")
        if not isinstance(notify, dict):
            continue
        if notify.get(event) is not True:
            continue
        try:
            parsed_group_id = int(group_id)
        except (TypeError, ValueError):
            continue
        selected[parsed_group_id] = notify.get("atAll") is True

    return [
        {"group_id": group_id, "at_all": selected[group_id]}
        for group_id in sorted(selected)
    ]


def select_targets(snapshot: dict, room_id: int, event: str) -> list[int]:
    """Select groups that strictly enable ``event`` for ``room_id``."""
    return [
        item["group_id"]
        for item in select_targets_detailed(snapshot, room_id, event)
    ]


def select_guest_targets(snapshot: dict, uid: str) -> list[int]:
    """Select groups that enable guest-entry notices and watch ``uid``."""
    groups = snapshot.get("groups")
    if not isinstance(groups, dict):
        return []

    selected: set[int] = set()
    uid_text = str(uid)

    for group_id, group in groups.items():
        if not isinstance(group, dict):
            continue
        if group.get("guestEntryEnabled") is not True:
            continue
        watched_users = group.get("watchedUsers", [])
        if not isinstance(watched_users, list):
            continue

        matched = False
        for item in watched_users:
            if not isinstance(item, dict) or "uid" not in item:
                continue
            if str(item.get("uid")) == uid_text:
                matched = True
                break
        if not matched:
            continue

        try:
            selected.add(int(group_id))
        except (TypeError, ValueError):
            continue

    return sorted(selected)


def dedup_key(event: str, room_id: int | str, event_id: str | int) -> str:
    """Build the exact cross-service deduplication key."""
    return f"qqnotify:dedup:{event}:{room_id}:{event_id}"


async def _get_session() -> Any:
    global _session
    if _session is None or getattr(_session, "closed", False):
        import aiohttp

        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
    return _session


def _stale_snapshot(now: float) -> dict | None:
    if _cache_snapshot is None or _cache_success_at is None:
        return None
    if now - _cache_success_at > STALE_LIMIT_SECONDS:
        return None
    return _cache_snapshot


def _warn_failure(reason: str, now: float) -> dict | None:
    global _last_failure_reason
    _last_failure_reason = reason
    stale = _stale_snapshot(now)
    if stale is not None:
        LOGGER.warning(
            "订阅拉取失败，使用未超过 %d 秒的旧快照: %s",
            STALE_LIMIT_SECONDS,
            reason,
        )
        return stale
    LOGGER.warning("订阅拉取失败且无可用旧快照，禁止发送: %s", reason)
    return None


async def get_snapshot(*, force: bool = False) -> dict | None:
    """Fetch a subscription snapshot with fresh-cache and stale fallback rules."""
    global _cache_snapshot, _cache_success_at, _last_failure_reason

    if not NOTIFY_API_TOKEN:
        _last_failure_reason = "NOTIFY_API_TOKEN 未配置"
        LOGGER.warning("NOTIFY_API_TOKEN 未配置，订阅不可用，禁止发送")
        return None

    now = time.monotonic()
    if (
        not force
        and _cache_snapshot is not None
        and _cache_success_at is not None
        and now - _cache_success_at <= CACHE_TTL_SECONDS
    ):
        return _cache_snapshot

    # ⚠️ 会话创建也必须被保护（2026-10-05 落盘复核时补）：
    # `_get_session()` 里是 `import aiohttp` + `ClientSession(...)`，两者都可能抛
    # （库不可用、或跨事件循环创建）。若让它逸出，`targets_for()` 会**抛异常而不是返回 None**，
    # 而 `None` 正是 D-62「读不到订阅 → 拒绝发送」的载体——契约会在最需要它的场景失效。
    try:
        session = await _get_session()
    except Exception as exc:
        return _warn_failure(
            f"HTTP 会话创建失败 {type(exc).__name__}", time.monotonic()
        )

    url = f"{NOTIFY_API_URL}{SUBSCRIPTIONS_PATH}"
    headers = {"Authorization": f"Bearer {NOTIFY_API_TOKEN}"}

    try:
        async with session.get(url, headers=headers, timeout=5) as response:
            if response.status != 200:
                return _warn_failure(f"HTTP {response.status}", time.monotonic())
            try:
                payload = await response.json()
            except Exception as exc:
                return _warn_failure(
                    f"JSON 解析异常 {type(exc).__name__}", time.monotonic()
                )
    except Exception as exc:
        return _warn_failure(f"网络异常 {type(exc).__name__}", time.monotonic())

    snapshot = parse_snapshot(payload)
    if snapshot is None:
        return _warn_failure("订阅快照版本或结构不兼容", time.monotonic())

    _cache_snapshot = snapshot
    _cache_success_at = time.monotonic()
    _last_failure_reason = None
    return snapshot


async def targets_detailed(
    room_id: int, event: str
) -> list[dict] | None:
    """Return detailed targets, preserving ``None`` when subscriptions fail."""
    snapshot = await get_snapshot()
    if snapshot is None:
        return None
    return select_targets_detailed(snapshot, room_id, event)


async def targets_for(room_id: int, event: str) -> list[int] | None:
    """Return target groups, preserving ``None`` for unavailable subscriptions."""
    detailed = await targets_detailed(room_id, event)
    if detailed is None:
        return None
    return [item["group_id"] for item in detailed]


async def guest_targets(uid: str) -> list[int] | None:
    """Return guest-entry target groups, preserving unavailable semantics."""
    snapshot = await get_snapshot()
    if snapshot is None:
        return None
    return select_guest_targets(snapshot, uid)


async def close() -> None:
    """Close the lazily-created HTTP session, if any."""
    global _session
    session = _session
    _session = None
    if session is not None and not getattr(session, "closed", False):
        await session.close()


def reset_cache() -> None:
    """Clear snapshot cache state. Intended for deterministic tests and self-checks."""
    global _cache_snapshot, _cache_success_at, _last_failure_reason
    _cache_snapshot = None
    _cache_success_at = None
    _last_failure_reason = None


def _failure_reason() -> str | None:
    """Return the latest fetch failure reason for the read-only self-check."""
    return _last_failure_reason
