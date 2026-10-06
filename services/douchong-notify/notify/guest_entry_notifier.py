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
"""Per-group VIP guest-entry notification orchestration (R-17)."""

from __future__ import annotations

import logging
import os

from . import dedup, sender, sub_client
from ._text import one_line

LOGGER = logging.getLogger("bili_douchong.notify")

_FALSE_VALUES = {"0", "false", "no", "off"}

# 空值 = 开启：与 NOTIFY_SC_ENABLED / NOTIFY_LIVE_ENABLED 同语义。
# 只有显式写 0 / false / no / off 才是关（不要写成"空值=关"，那是 D-91 的坑）。
NOTIFY_GUEST_ENTRY_ENABLED: bool = (
    os.getenv("NOTIFY_GUEST_ENTRY_ENABLED", "1").lower() not in _FALSE_VALUES
)

# R-17 规格：同一场次内同一用户只推一次。TTL 取 8h —— 一场直播通常不超过
# 8 小时，且 session_id 已经把不同场次隔开，所以 8h 只是一个安全上限，
# 过期后不存在"同场次重发"的问题。
DEDUP_TTL_SECONDS = 8 * 60 * 60


def build_guest_entry_text(*, uname: str, room_id: int) -> str:
    """纯函数：构造特关进房文案。

    用户名兜底放在这里（文案的唯一出口）—— 与 `build_sc_text` 同一约定：
    不依赖调用方先做 `or "未采集"`。否则任何新调用方漏一次兜底，
    群里就会看到「 进入了直播间」这种空名字。
    """
    safe_uname = one_line(uname, 40) or "未采集"
    return (
        f"【特关进房】\n{safe_uname} 进入了直播间\n"
        f"https://live.bilibili.com/{room_id}"
    )


def _summary(
    *,
    sent: bool = False,
    skipped: str | None,
    targets: list[int],
    reason: str | None = None,
) -> dict:
    return {"sent": sent, "skipped": skipped, "targets": targets, "reason": reason}


def _unavailable_reason() -> str:
    """取订阅层最近一次失败原因（只读；取不到就给通用文案）。

    ⚠️ `getter()` 只调一次并保存结果 —— 连续调两次（`callable(getter) and getter()`）
    会拿到两个不同时刻的值，在并发下可能一个为真一个为假，产生自相矛盾的分支。
    """
    getter = getattr(sub_client, "_failure_reason", None)
    if callable(getter):
        reason = getter()
        if reason:
            return str(reason)
    return "订阅不可用"


async def notify_guest_entry(
    room_id: int,
    *,
    uid: int,
    uname: str,
    session_id: int | str,
) -> dict:
    """发送特关进房通知，永远以摘要 dict 返回、**不向外抛异常**。

    参数
    ----
    room_id    : 直播间 ID。
    uid        : 进房用户 UID（用于匹配该群的 watchedUsers）。
    uname      : 用户名；空时显示「未采集」。
    session_id : 当前场次。调用方（event_ingestion）已保证非 None —— R-17 规格
                 要求「取不到场次则不推送」。
    """
    try:
        return await _notify_guest_entry(
            room_id, uid=uid, uname=uname, session_id=session_id
        )
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK
        LOGGER.warning("特关进房通知异常 room=%s uid=%s: %s", room_id, uid, exc)
        return _summary(skipped="exception", targets=[], reason=str(exc))


async def _notify_guest_entry(
    room_id: int,
    *,
    uid: int,
    uname: str,
    session_id: int | str,
) -> dict:
    if not NOTIFY_GUEST_ENTRY_ENABLED:
        return _summary(skipped="disabled", targets=[])

    # uid=0 表示未采集身份 —— 没有身份就无从匹配 watchedUsers。
    # 上游 handler 已过滤，这里是**独立防线**：通知层不能假设上游永远合法。
    if not uid:
        return _summary(skipped="no_uid", targets=[])

    # 按 uid 查（guest_targets → select_guest_targets），不是按事件查：
    # 特关进房是按群的 guestEntryEnabled + watchedUsers 决定，与主播订阅开关无关。
    target_ids = await sub_client.guest_targets(str(uid))
    if target_ids is None:
        # None 与 [] 必须区分：None = 订阅拉不到（D-62：拒绝发送），
        # [] = 能拉到但没人订阅这个用户（正常跳过）。
        return _summary(
            skipped="unavailable", targets=[], reason=_unavailable_reason()
        )
    if not target_ids:
        return _summary(skipped="no_targets", targets=[])

    # 去重键：qqnotify:dedup:guestEntry:{room_id}:{uid}:{session_id}
    # 复合 id 把"同一场次内同一用户重复进房"折叠成一次。
    composite_event_id = f"{uid}:{session_id}"
    first = await dedup.first_seen(
        "guestEntry", room_id, composite_event_id, DEDUP_TTL_SECONDS
    )
    if not first:
        return _summary(skipped="duplicate", targets=target_ids)

    # 特关进房**不带 atAll**：atAll 是 AnchorSubscription.notify 的字段（按主播配），
    # 而特关进房是按群配的（guestEntryEnabled），没有对应的群级 atAll 开关（D-99 范围）。
    text = build_guest_entry_text(uname=uname, room_id=room_id)
    base_rich = sender.build_text_message(text)
    fallback = sender.build_fallback_text(text)
    sent_any = False

    for group_id in target_ids:
        try:
            ok = await sender.send_group(group_id, list(base_rich), fallback)
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK
            ok = False
            LOGGER.warning(
                "特关进房发送异常 group=%s uid=%s: %s", group_id, uid, exc
            )
        if ok:
            sent_any = True
        else:
            LOGGER.warning(
                "特关进房发送失败 group=%s room=%s uid=%s", group_id, room_id, uid
            )

    return _summary(sent=sent_any, skipped=None, targets=target_ids)
