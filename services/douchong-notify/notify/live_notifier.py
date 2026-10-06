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

"""Per-subscription live/liveEnd notification orchestration."""

from __future__ import annotations

import logging
import os

from . import dedup, dynamic_render, sender, sub_client

LOGGER = logging.getLogger("bili_douchong.notify")

_FALSE_VALUES = {"0", "false", "no", "off"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
NOTIFY_LIVE_ENABLED = (
    os.getenv("NOTIFY_LIVE_ENABLED", "1").lower() not in _FALSE_VALUES
)
INITIAL_SILENT = (
    os.getenv("BILI_NOTIFY_INITIAL_SILENT", "1").lower() in _TRUE_VALUES
)
DEDUP_TTL_SECONDS = 24 * 60 * 60


def _one_line(value: object, limit: int) -> str:
    text = str(value or "")
    text = (
        text.replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def build_live_text(
    *,
    name: str,
    title: str,
    area: str,
    time_text: str,
    room_id: int,
) -> str:
    """Build the upstream-compatible live-start text."""
    safe_name = _one_line(name, 40)
    safe_title = _one_line(title, 80)
    safe_area = _one_line(area, 40)
    return (
        f"【开播通知】\n{safe_name} 开播啦~\n"
        f"直播标题：{safe_title}\n直播分区：{safe_area}\n"
        f"开播时间：{time_text}\n"
        f"直播间：https://live.bilibili.com/{room_id}"
    )


def build_live_off_text(*, name: str, duration_text: str) -> str:
    """Build the upstream-compatible live-end text."""
    safe_name = _one_line(name, 40)
    return f"【下播通知】\n{safe_name} 下播了\n本场时长：{duration_text}"


def _summary(
    *,
    sent: bool = False,
    skipped: str | None,
    targets: list[int],
    reason: str | None = None,
) -> dict:
    return {
        "sent": sent,
        "skipped": skipped,
        "targets": targets,
        "reason": reason,
    }


def _unavailable_reason() -> str:
    getter = getattr(sub_client, "_failure_reason", None)
    if callable(getter):
        reason = getter()
        if reason:
            return str(reason)
    return "订阅不可用"

async def _build_notice_rich(
    *,
    event: str,
    room_id: int,
    data: dict,
    text: str,
    cover: str,
) -> list[dict]:
    """Prefer a rendered card, but always preserve a sendable text path."""
    inline_builder = getattr(
        sender,
        "build_inline_image_message",
        None,
    )
    if not callable(inline_builder):
        # Compatibility with legacy/custom sender implementations.
        return sender.build_text_message(text, cover)

    image_bytes = None
    try:
        if event == "live":
            cover_bytes = None
            fetcher = getattr(sender, "fetch_image_bytes", None)
            if cover and callable(fetcher):
                cover_bytes = await fetcher(cover)

            render_data = dict(data)
            render_data["room_id"] = room_id
            image_bytes = dynamic_render.render_live_start(
                render_data,
                cover_bytes=cover_bytes,
            )
        else:
            image_bytes = dynamic_render.render_live_end(data)
    except Exception as exc:
        LOGGER.warning(
            "直播通知卡片构建异常 event=%s: %s",
            event,
            type(exc).__name__,
        )
        image_bytes = None

    if image_bytes is not None:
        rich = inline_builder(image_bytes)
        if rich:
            if event == "live":
                rich.append(
                    {
                        "type": "text",
                        "data": {
                            "text": (
                                "\nhttps://live.bilibili.com/"
                                f"{room_id}"
                            )
                        },
                    }
                )
            return rich

    # Card rendering is optional.  Real card-capable senders fall back to the
    # exact legacy text body; the transport-level fallback below remains intact.
    return sender.build_text_message(text)

async def _notify(
    room_id: int,
    *,
    event: str,
    data: dict,
    initial: bool = False,
    session_id: int | str | None = None,
) -> dict:
    if not NOTIFY_LIVE_ENABLED:
        return _summary(skipped="disabled", targets=[])

    if initial and INITIAL_SILENT:
        return _summary(skipped="initial_silent", targets=[])

    detailed = await sub_client.targets_detailed(room_id, event)
    if detailed is None:
        return _summary(
            skipped="unavailable",
            targets=[],
            reason=_unavailable_reason(),
        )

    target_ids = [item["group_id"] for item in detailed]
    if not detailed:
        return _summary(skipped="no_targets", targets=[])

    if session_id is None:
        LOGGER.warning(
            "通知缺少 session_id，跳过去重并继续发送 event=%s room=%s",
            event,
            room_id,
        )
    else:
        first = await dedup.first_seen(
            event,
            room_id,
            session_id,
            DEDUP_TTL_SECONDS,
        )
        if not first:
            return _summary(
                skipped="duplicate",
                targets=target_ids,
            )

    if event == "live":
        text = build_live_text(
            name=data.get("name") or "",
            title=data.get("title") or "",
            area=data.get("area") or "",
            time_text=data.get("time_text") or "",
            room_id=room_id,
        )
        cover = str(data.get("cover") or "")
    else:
        text = build_live_off_text(
            name=data.get("name") or "",
            duration_text=data.get("duration_text") or "",
        )
        cover = ""

    base_rich = await _build_notice_rich(
        event=event,
        room_id=room_id,
        data=data,
        text=text,
        cover=cover,
    )
    fallback = sender.build_fallback_text(text, cover)
    sent_any = False

    for item in detailed:
        rich = list(base_rich)
        if item.get("at_all") is True:
            rich.insert(
                0,
                {"type": "at", "data": {"qq": "all"}},
            )

        try:
            ok = await sender.send_group(
                item["group_id"],
                rich,
                fallback,
            )
        except Exception as exc:
            ok = False
            LOGGER.warning(
                "群通知发送异常 group=%s event=%s: %s",
                item["group_id"],
                event,
                type(exc).__name__,
            )

        if ok:
            sent_any = True
        else:
            LOGGER.warning(
                "群通知发送失败 group=%s event=%s",
                item["group_id"],
                event,
            )

    return _summary(
        sent=sent_any,
        skipped=None,
        targets=target_ids,
    )


async def notify_live(
    room_id: int,
    *,
    data: dict,
    initial: bool = False,
    session_id: int | str | None = None,
) -> dict:
    """Send a live-start notification to groups that enable ``live``."""
    return await _notify(
        room_id,
        event="live",
        data=data,
        initial=initial,
        session_id=session_id,
    )


async def notify_live_off(
    room_id: int,
    *,
    data: dict,
    session_id: int | str | None = None,
) -> dict:
    """Send a live-end notification to groups that enable ``liveEnd``."""
    return await _notify(
        room_id,
        event="liveEnd",
        data=data,
        session_id=session_id,
    )


async def close() -> None:
    """Close notification-layer connections in dependency order."""
    await sub_client.close()
    await dedup.close()
    await sender.close()
