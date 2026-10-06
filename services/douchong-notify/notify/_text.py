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
"""Notification-layer shared text helpers."""

from __future__ import annotations


def one_line(value: object, limit: int) -> str:
    """压单行并按**码点**截断。

    与 `live_notifier._one_line` / `sc_notifier._one_line` **同约定**：
    先把 CRLF / CR / LF 统一替换成空格再 strip，最后按码点截断 ——
    按字节截断会把中文切成半个字，出现乱码。

    为什么抽成独立模块而不是复制第三遍：特关进房是第三个需要它的 notifier。
    已上线的 live / sc 两处保留各自私有实现不动（避免对已上线代码产生
    无功能性的整文件 diff），**新代码一律用本模块**。
    """
    text = (
        str(value or "")
        .replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )
    if len(text) > limit:
        return text[:limit] + "…"
    return text
