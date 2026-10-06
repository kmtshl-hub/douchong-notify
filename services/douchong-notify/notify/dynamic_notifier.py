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
"""Per-subscription dynamic notification orchestration (R-18)."""

from __future__ import annotations

import logging
import os

from . import dedup, dynamic_render, sender, sub_client
from ._text import one_line

LOGGER = logging.getLogger("bili_douchong.notify")

_FALSE_VALUES = {"0", "false", "no", "off"}
NOTIFY_DYNAMIC_ENABLED: bool = (
    os.getenv("NOTIFY_DYNAMIC_ENABLED", "1").lower() not in _FALSE_VALUES
)
DEDUP_TTL_SECONDS = 48 * 60 * 60
DYNAMIC_EVENT = "dynamic"
DYNAMIC_URL_PREFIX = "https://t.bilibili.com/"


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


def _normalize_dynamic_id(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if value > 0 else ""
    if isinstance(value, str):
        return value.strip()
    return ""


def build_dynamic_fallback_text(
    *,
    dynamic_id: str,
    uname: object,
    text: object,
) -> str:
    """Build bounded plain-text fallback containing the original dynamic URL."""
    safe_uname = one_line(uname, 40) or "未知用户"
    safe_text = one_line(text, 240)

    lines = [
        "【动态通知】",
        f"{safe_uname} 发布了新动态",
    ]
    if safe_text:
        lines.append(f"内容：{safe_text}")

    lines.append(
        f"原动态：{DYNAMIC_URL_PREFIX}{dynamic_id}"
    )
    return "\n".join(lines)


async def notify_dynamic(
    room_id: int,
    *,
    data: object,
) -> dict:
    """Send one dynamic notice and never propagate notification-side exceptions."""
    try:
        return await _notify_dynamic(
            room_id,
            data=data,
        )
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK
        LOGGER.warning(
            "动态通知异常 room=%s: %s",
            room_id,
            exc,
        )
        return _summary(
            skipped="exception",
            targets=[],
            reason=str(exc),
        )


async def _notify_dynamic(
    room_id: int,
    *,
    data: object,
) -> dict:
    # Total switch must be the first external-side-effect gate: when disabled we do
    # not query subscriptions, Redis, render Pillow content, or touch OneBot.
    if not NOTIFY_DYNAMIC_ENABLED:
        return _summary(
            skipped="disabled",
            targets=[],
        )

    if not isinstance(data, dict):
        return _summary(
            skipped="invalid_data",
            targets=[],
        )

    dynamic_id = _normalize_dynamic_id(
        data.get("dynamic_id")
    )
    if not dynamic_id:
        return _summary(
            skipped="no_dynamic_id",
            targets=[],
        )

    detailed = await sub_client.targets_detailed(
        room_id,
        DYNAMIC_EVENT,
    )
    if detailed is None:
        return _summary(
            skipped="unavailable",
            targets=[],
            reason=_unavailable_reason(),
        )

    target_ids = [
        item["group_id"]
        for item in detailed
    ]

    if not detailed:
        # Do not consume the 48h dedup key before there is anybody to notify.
        return _summary(
            skipped="no_targets",
            targets=[],
        )

    try:
        first = await dedup.first_seen(
            DYNAMIC_EVENT,
            room_id,
            dynamic_id,
            DEDUP_TTL_SECONDS,
        )
    except Exception as exc:
        # dedup.py itself is fail-open. Preserve that contract even if a replacement
        # implementation unexpectedly raises instead of returning True.
        LOGGER.warning(
            "动态通知去重异常，fail open 继续发送 room=%s dynamic=%s: %s",
            room_id,
            dynamic_id,
            type(exc).__name__,
        )
        first = True

    if not first:
        return _summary(
            skipped="duplicate",
            targets=target_ids,
        )

    fallback = build_dynamic_fallback_text(
        dynamic_id=dynamic_id,
        uname=data.get("uname"),
        text=data.get("text"),
    )

    card: bytes | None
    try:
        card = dynamic_render.render_dynamic(data)
    except Exception as exc:
        # Renderer isolation is mandatory: never drop the notice just because image
        # generation regressed.
        LOGGER.warning(
            "动态卡片渲染器抛异常，回落纯文本 room=%s dynamic=%s: %s",
            room_id,
            dynamic_id,
            type(exc).__name__,
        )
        card = None

    if card:
        rich_base = sender.build_inline_image_message(card)
        if not rich_base:
            LOGGER.warning(
                "动态卡片消息构造失败，回落纯文本 room=%s dynamic=%s",
                room_id,
                dynamic_id,
            )
            rich_base = sender.build_text_message(fallback)
    else:
        rich_base = sender.build_text_message(fallback)

    sent_any = False

    for item in detailed:
        # Every group receives a fresh list. Without this copy, inserting @all into
        # one group's message would leak it into all following groups.
        rich = list(rich_base)

        if item.get("at_all") is True:
            rich.insert(
                0,
                {
                    "type": "at",
                    "data": {"qq": "all"},
                },
            )

        try:
            ok = await sender.send_group(
                item["group_id"],
                rich,
                fallback,
            )
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK
            ok = False
            LOGGER.warning(
                "动态群通知发送异常 group=%s dynamic=%s: %s",
                item.get("group_id"),
                dynamic_id,
                type(exc).__name__,
            )

        if ok:
            sent_any = True
        else:
            LOGGER.warning(
                "动态群通知发送失败 group=%s dynamic=%s",
                item.get("group_id"),
                dynamic_id,
            )

    return _summary(
        sent=sent_any,
        skipped=None,
        targets=target_ids,
    )
