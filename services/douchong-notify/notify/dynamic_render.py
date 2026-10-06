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

"""
Rendering stub for the open-source distribution.
渲染器 stub —— 开源版本不包含图片卡片渲染的完整实现。

The image-card renderer (Bilibili-dynamic-style PNG cards) is intentionally
NOT part of this open-source release.  All callers already handle a ``None``
return value by falling back to a plain-text message, so providing this stub
keeps the extension fully functional (text-only) without any change to the
calling modules.

调用方（``live_notifier`` / ``dynamic_notifier``）均显式检查返回值：
``if image_bytes is not None:`` / ``if card:`` ⇒ 返回 ``None`` 即自动降级为纯文本。

``_load_pillow`` 也一并提供（恒返回 ``None``）：调用方与测试会引用该私有钩子，
缺少它会产生 ``AttributeError``。
"""
from __future__ import annotations


def _load_pillow() -> None:
    """Stub for the real lazy loader: always reports "Pillow unavailable"."""
    return None


def render_dynamic(data: object) -> bytes | None:  # noqa: ARG001
    """Return ``None`` so callers fall back to plain text."""
    return None


def render_live_start(data: object, **_kwargs: object) -> bytes | None:  # noqa: ARG001
    """Return ``None`` so callers fall back to plain text."""
    return None


def render_live_end(data: object) -> bytes | None:  # noqa: ARG001
    """Return ``None`` so callers fall back to plain text."""
    return None
