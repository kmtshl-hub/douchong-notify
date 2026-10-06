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
"""Read-only subscription self-check. Never sends messages or writes Redis."""

from __future__ import annotations

import asyncio
import sys

from . import sub_client

_FLAG_NAMES = ("dynamic", "live", "liveEnd", "sc", "atAll")


def _one_line(value: object, limit: int = 80) -> str:
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def _flag(value: object) -> str:
    if value is True:
        return "开"
    if value is False:
        return "关"
    return "无效"


async def _run_check() -> int:
    print("== 斗虫通知订阅自检 ==")
    print(f"QQBOT_NOTIFY_URL : {sub_client.NOTIFY_API_URL}")
    print(
        "NOTIFY_API_TOKEN : "
        + ("已配置" if sub_client.NOTIFY_API_TOKEN else "未配置")
    )
    print()

    if not sub_client.NOTIFY_API_TOKEN:
        print("[失败] NOTIFY_API_TOKEN 未配置；未发起无鉴权请求。")
        return 1

    sub_client.reset_cache()
    snapshot = await sub_client.get_snapshot(force=True)
    if snapshot is None:
        reason = sub_client._failure_reason() or "未知原因"
        print(f"[失败] 订阅不可用：{_one_line(reason, 160)}")
        return 1

    groups = snapshot.get("groups", {})
    print("[HTTP] 拉取成功")
    print(f"version : {snapshot.get('version')}")
    print(f"群数量  : {len(groups)}")
    print()

    print("== 群 / 房间开关 ==")
    room_ids: set[str] = set()
    watched_uids: set[str] = set()

    for group_id in sorted(groups, key=lambda value: str(value)):
        group = groups.get(group_id)
        print(f"群 {_one_line(group_id)}")
        if not isinstance(group, dict):
            print("  anchors: <无效群结构>")
            continue

        anchors = group.get("anchors", {})
        if not isinstance(anchors, dict) or not anchors:
            print("  anchors: <空>")
        else:
            for room_id in sorted(anchors, key=lambda value: str(value)):
                room_ids.add(str(room_id))
                anchor = anchors.get(room_id)
                notify = anchor.get("notify", {}) if isinstance(anchor, dict) else {}
                if not isinstance(notify, dict):
                    notify = {}
                flags = "  ".join(
                    f"{name}={_flag(notify.get(name))}" for name in _FLAG_NAMES
                )
                print(f"  房间 {_one_line(room_id)}  {flags}")

        watched_users = group.get("watchedUsers", [])
        if isinstance(watched_users, list):
            for item in watched_users:
                if isinstance(item, dict) and "uid" in item:
                    watched_uids.add(str(item.get("uid")))

    print()
    print("== 被监控房间目标群 ==")
    for room_id in sorted(room_ids):
        try:
            room_value = int(room_id)
        except ValueError:
            print(f"房间 {_one_line(room_id)}  <非法房间号，跳过目标计算>")
            continue
        live_groups = sub_client.select_targets(snapshot, room_value, "live")
        end_groups = sub_client.select_targets(snapshot, room_value, "liveEnd")
        sc_groups = sub_client.select_targets(snapshot, room_value, "sc")
        print(
            f"房间 {_one_line(room_id)}  live={live_groups}  "
            f"liveEnd={end_groups}  sc={sc_groups}"
        )
    if not room_ids:
        print("<无被监控房间>")

    print()
    print("== 特关用户目标群 ==")
    for uid in sorted(watched_uids):
        groups_for_uid = sub_client.select_guest_targets(snapshot, uid)
        print(f"UID {_one_line(uid)}  guestEntry={groups_for_uid}")
    if not watched_uids:
        print("<无特关用户>")

    return 0


async def main() -> int:
    """Run the self-check and close its private HTTP session on the same loop."""
    try:
        return await _run_check()
    finally:
        await sub_client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
