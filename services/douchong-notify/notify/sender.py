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
"""Reusable OneBot group-message sender with plain-text fallback."""

from __future__ import annotations

import logging
import os
from typing import Any

LOGGER = logging.getLogger("bili_douchong.notify")

ONEBOT_HTTP_URL = os.getenv("ONEBOT_HTTP_URL", "http://127.0.0.1:13000").rstrip("/")
ONEBOT_HTTP_TOKEN = os.getenv("ONEBOT_HTTP_TOKEN", "")

_session: Any | None = None


def build_text_message(text: str, cover: str = "") -> list[dict]:
    """Build the existing text-plus-optional-image OneBot message shape."""
    message = [{"type": "text", "data": {"text": text}}]
    if cover:
        message.append({"type": "image", "data": {"file": cover}})
    return message


def build_fallback_text(text: str, cover: str = "") -> str:
    """Build the existing plain-text fallback, preserving the cover URL."""
    if cover:
        return f"{text}\n封面：{cover}"
    return text


def build_inline_image_message(
    image_bytes: object,
) -> list[dict]:
    """Build a OneBot v11 inline base64 image segment from raw image bytes.

    ``base64.b64encode`` emits a compact ASCII string without line wrapping, so the
    ``file`` value never contains whitespace. Invalid/empty inputs return an empty
    list, allowing callers to fall back to text without raising.
    """
    if not isinstance(
        image_bytes,
        (bytes, bytearray, memoryview),
    ):
        return []

    raw = bytes(image_bytes)
    if not raw:
        return []

    import base64

    encoded = base64.b64encode(raw).decode("ascii")

    return [
        {
            "type": "image",
            "data": {
                "file": f"base64://{encoded}"
            },
        }
    ]

async def fetch_image_bytes(
    url: object,
    *,
    max_bytes: int = 8 * 1024 * 1024,
) -> bytes | None:
    """Download an HTTP(S) image body with a hard size cap.

    Failure is deliberately represented as ``None`` so card rendering can continue
    without the cover.  OneBot authorization headers are never forwarded to the
    external image host.
    """
    if not isinstance(url, str):
        return None
    target = url.strip()
    if not target.startswith(("http://", "https://")):
        return None
    if max_bytes <= 0:
        return None

    try:
        session = await _get_session()
        async with session.get(target, timeout=10) as response:
            if response.status >= 400:
                return None

            raw_length = response.headers.get("Content-Length")
            if raw_length:
                try:
                    if int(raw_length) > max_bytes:
                        return None
                except (TypeError, ValueError):
                    pass

            body = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                body.extend(chunk)
                if len(body) > max_bytes:
                    return None
            return bytes(body) if body else None
    except Exception as exc:
        LOGGER.warning(
            "直播封面下载失败: %s",
            type(exc).__name__,
        )
        return None

async def _get_session() -> Any:
    global _session
    if _session is None or getattr(_session, "closed", False):
        import aiohttp

        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    return _session


def _headers() -> dict[str, str]:
    if ONEBOT_HTTP_TOKEN:
        return {"Authorization": f"Bearer {ONEBOT_HTTP_TOKEN}"}
    return {}


async def _post_message(session: Any, group_id: int, message: object) -> bool:
    url = f"{ONEBOT_HTTP_URL}/send_group_msg"
    body = {"group_id": group_id, "message": message}
    try:
        async with session.post(
            url,
            json=body,
            headers=_headers(),
            timeout=10,
        ) as response:
            try:
                payload = await response.json()
            except Exception as exc:
                LOGGER.warning(
                    "OneBot 响应 JSON 解析失败: %s", type(exc).__name__
                )
                return False
            return (
                response.status < 400
                and payload.get("retcode", 0) == 0
                and payload.get("status", "ok") == "ok"
            )
    except Exception as exc:
        LOGGER.warning("OneBot 发送请求失败: %s", type(exc).__name__)
        return False


async def send_group(
    group_id: int,
    rich: list[dict],
    fallback: str,
    *,
    session: Any = None,
) -> bool:
    """Send rich content once, then retry once with plain text on failure."""
    # 与 sub_client 同理：会话创建失败也要落回返回值而不是抛异常（调用方按 bool 判断成败）。
    try:
        active_session = session if session is not None else await _get_session()
    except Exception as exc:
        LOGGER.warning("OneBot 会话创建失败: %s", type(exc).__name__)
        return False

    if await _post_message(active_session, group_id, rich):
        return True

    LOGGER.warning("OneBot 富消息发送失败，尝试纯文本兜底")
    return await _post_message(active_session, group_id, fallback)


async def close() -> None:
    """Close the lazily-created HTTP session, if any."""
    global _session
    session = _session
    _session = None
    if session is not None and not getattr(session, "closed", False):
        await session.close()
