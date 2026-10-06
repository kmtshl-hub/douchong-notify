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
"""Per-subscription SC notification orchestration (R-16)."""

from __future__ import annotations

import logging
import os

from . import dedup, sender, sub_client

LOGGER = logging.getLogger("bili_douchong.notify")

_FALSE_VALUES = {"0", "false", "no", "off"}

# 空值 = 开启：与 live_notifier.NOTIFY_LIVE_ENABLED 同语义（D-91 的反面 ——
# 这里「缺失」表示按默认工作，只有显式写 0/false/no/off 才是关）。
NOTIFY_SC_ENABLED: bool = (
    os.getenv("NOTIFY_SC_ENABLED", "1").lower() not in _FALSE_VALUES
)

DEFAULT_SC_MIN_PRICE = 30
DEDUP_TTL_SECONDS = 24 * 60 * 60  # R-16 规格：TTL 24h


def parse_min_price(raw: object, default: int = DEFAULT_SC_MIN_PRICE) -> int:
    """容错解析价格阈值：非法值回落默认并留痕，**绝不因此让模块导入失败**。

    为什么不在模块级直接写 `int(os.getenv(...))`：那一行在 import 时执行，
    环境变量一旦被写成 `30元` 之类的非法值，整个 `sc_notifier` 会导入失败 →
    `event_ingestion._schedule_sc_notify()` 里的 `from .notify import sc_notifier`
    抛异常 → SC 通知**静默停摆**（而且只留一条 warning）。
    """
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        LOGGER.warning("BILI_SC_MIN_PRICE 非法（%r），回落默认 %d", raw, default)
        return default
    return value if value >= 0 else default


# 全局价格阈值。⚠️ D-68 写明「按群按主播配置优先于全局」，但该数据通路尚不存在
# （快照 NotifyFlags 没有 scMinPrice 字段）⇒ Batch 4a 是**已知降级**，只做全局。
SC_MIN_PRICE: int = parse_min_price(os.getenv("BILI_SC_MIN_PRICE"))


def _normalize_price(value: object) -> int:
    """把上游价格收敛成非负整数：坏数据回落 0，绝不把 `None` 渲染进文案。

    上游 `message.price` 正常是 int，但通知层是**副作用链路**，
    不能假设上游字段永远合法 —— 一旦插值出 `¥None` 就是用户可见事故。
    """
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _one_line(value: object, limit: int) -> str:
    """压单行并按码点截断 —— 与 `live_notifier._one_line` 同约定。"""
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


def build_sc_text(
    *,
    uname: str,
    price: int,
    message: str,
    room_id: int,
) -> str:
    """纯函数：构造 SC 通知文案。

    用户名兜底放在这里（文案的唯一出口），不依赖调用方先做 `or "未采集"` ——
    否则任何新调用方漏一次兜底，群里就会看到「 发送了 ¥50 醒目留言」这种空名字。
    """
    safe_uname = _one_line(uname, 40) or "未采集"
    safe_msg = _one_line(message, 200)
    safe_price = _normalize_price(price)
    lines = [f"【SC 醒目留言】\n{safe_uname} 发送了 ¥{safe_price} 醒目留言"]
    if safe_msg:
        lines.append(f"内容：{safe_msg}")
    lines.append(f"直播间：https://live.bilibili.com/{room_id}")
    return "\n".join(lines)


def _summary(
    *,
    sent: bool = False,
    skipped: str | None,
    targets: list[int],
    reason: str | None = None,
) -> dict:
    return {"sent": sent, "skipped": skipped, "targets": targets, "reason": reason}


def _unavailable_reason() -> str:
    """取订阅层最近一次失败原因（只读；取不到就给通用文案）。"""
    getter = getattr(sub_client, "_failure_reason", None)
    if callable(getter):
        reason = getter()
        if reason:
            return str(reason)
    return "订阅不可用"


async def notify_sc(
    room_id: int,
    *,
    sc_id: str | int,
    uname: str,
    uid: int,
    price: int,
    message: str,
    session_id: int | str | None = None,
) -> dict:
    """发送 SC 醒目留言通知，永远以摘要 dict 返回、**不向外抛异常**。

    参数
    ----
    sc_id      : 去重键。调用方传上游构好的 `event_key`（`sc:{room}:{id}` 或含正文哈希的兜底）。
    uname      : 用户名；空时显示「未采集」。
    uid        : 用户 UID；0 表示未采集（仅入日志）。
    price      : SC 金额（CNY，整数）。
    message    : 留言正文；可为空。
    session_id : 当前场次；None 时跳过去重并继续发送（只留 warning）。
    """
    try:
        return await _notify_sc(
            room_id,
            sc_id=sc_id,
            uname=uname,
            uid=uid,
            price=price,
            message=message,
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK
        LOGGER.warning("SC 通知异常 room=%s sc_id=%s: %s", room_id, sc_id, exc)
        return _summary(skipped="exception", targets=[], reason=str(exc))


async def _notify_sc(
    room_id: int,
    *,
    sc_id: str | int,
    uname: str,
    uid: int,
    price: int,
    message: str,
    session_id: int | str | None,
) -> dict:
    if not NOTIFY_SC_ENABLED:
        return _summary(skipped="disabled", targets=[])

    price = _normalize_price(price)

    if price < SC_MIN_PRICE:
        return _summary(
            skipped="below_threshold",
            targets=[],
            reason=f"¥{price}<¥{SC_MIN_PRICE}",
        )

    detailed = await sub_client.targets_detailed(room_id, "sc")
    if detailed is None:
        return _summary(
            skipped="unavailable", targets=[], reason=_unavailable_reason()
        )

    target_ids = [item["group_id"] for item in detailed]
    if not detailed:
        return _summary(skipped="no_targets", targets=[])

    if session_id is None:
        LOGGER.warning(
            "SC 通知缺少 session_id，跳过去重 room=%s sc_id=%s", room_id, sc_id
        )
    else:
        first = await dedup.first_seen("sc", room_id, sc_id, DEDUP_TTL_SECONDS)
        if not first:
            return _summary(skipped="duplicate", targets=target_ids)

    text = build_sc_text(
        uname=uname,
        price=price,
        message=message,
        room_id=room_id,
    )
    base_rich = sender.build_text_message(text)
    fallback = sender.build_fallback_text(text)
    sent_any = False

    for item in detailed:
        rich = list(base_rich)
        if item.get("at_all") is True:
            rich.insert(0, {"type": "at", "data": {"qq": "all"}})
        try:
            ok = await sender.send_group(item["group_id"], rich, fallback)
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK
            ok = False
            LOGGER.warning(
                "SC 群通知发送异常 group=%s room=%s: %s",
                item["group_id"],
                room_id,
                exc,
            )
        if ok:
            sent_any = True
        else:
            LOGGER.warning(
                "SC 群通知发送失败 group=%s room=%s", item["group_id"], room_id
            )

    return _summary(sent=sent_any, skipped=None, targets=target_ids)
