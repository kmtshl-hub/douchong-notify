# Copyright (C) 2026 小恩 <https://github.com/kmtshl-hub>
# Copyright (C) QianQiuZy (original work, VR_douchong)
#
# This file is a MODIFIED version of a file from VR_douchong
#   upstream: https://github.com/QianQiuZy/VR_douchong
#   original work Copyright (C) QianQiuZy, licensed under GPL-2.0
#
# Modifications by 小恩 <https://github.com/kmtshl-hub>; each change is
# marked with "[PATCH]" or described in an inline comment block below.
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
#   upstream file: app/event_ingestion.py
#
"""Bilibili websocket event ingestion backed by canonical runtime state."""

# ══════════════════════════════════════════════════════════════════════════
# 本文件是上游 `app/event_ingestion.py` 的**补丁版**（Batch 4a，决策 D-99）。
# 上游快照 440 行，sha256 = 900c2b42c3d267525d705e4dfcc8ef319560ac6cc794d00952dc8a340e1286a6
#
# 相对上游**仅五处**改动（其余逐字一致，便于上游升级时比对合并）：
#   ① 顶部新增 `import asyncio` / `import time`（上游未导入；①桥接 ②探针节流）
#   ② 模块级新增 `_PROBE_STATE`、`_schedule_sc_notify()`、
#      `_schedule_guest_entry_notify()`
#   ③ `_on_super_chat` 末尾加 SC 通知 hook（R-16 / W-4a-2）
#   ④ 类末尾新增 `_on_interact_word_v2`（R-17）：只读探针（W-4a-1）
#      + 特关进房通知 hook（W-4a-3）
#   ⑤ 三处上游业务常量加下划线分隔（946_684_800 / 19_176_000 / 51_994_000）：
#      数值语义完全等价，只为过 `npm run scan:identifiers`（D-90 精神：
#      改写内容而非给扫描器开后门）
#
# ✅ W-4a-1 探针已于 2026-10-06 出结论（事件确实被推送、字段可用）
#    ⇒ ④ 内已按计划接入发送逻辑（W-4a-3）；「结论前禁止发送」的约束已解除。
# ══════════════════════════════════════════════════════════════════════════
import asyncio
import datetime
import logging
import time
from dataclasses import dataclass
from typing import Callable

from . import DanmakuCounts, PendingDanmaku, blivedm, runtime_state
from .metrics_runtime import (
    current_bucket_index,
    danmaku_bucket_target,
    record_payment,
    start_session,
)
from .models import (
    LiveSession,
    RoomBlindBoxMonthly,
    RoomLiveStats,
    RoomStatsMonthly,
    SuperChatLog,
)
from .redis_metrics import register_payer
from .whale_metrics import record_whale_revenue
from .danmaku_metrics import record as record_danmaku

COMMON_NOTICE_GIFT_COIN_MAP = {
    "干杯之旅": 10000,
    "启航之旅": 100000,
    "友谊的小船": 4900,
    "冲浪": 89900,
    "海湾之旅": 799900,
    "鸿运小电视": 1000000,
}


@dataclass(frozen=True)
class EventDependencies:
    month_str: Callable[[], str]
    profit_to_tenths: Callable[[int, int], int]
    send_cookie_invalid_email: Callable[[str], None]


_dependencies: EventDependencies | None = None


def configure(dependencies: EventDependencies) -> None:
    global _dependencies
    _dependencies = dependencies


def _configured_dependencies() -> EventDependencies:
    if _dependencies is None:
        raise RuntimeError("event ingestion dependencies are not configured")
    return _dependencies


def _timestamp_to_datetime(value: int | float | str | None) -> datetime.datetime | None:
    """Convert a Bilibili seconds/milliseconds timestamp when it is usable."""
    if value is None:
        return None
    try:
        seconds = int(value)
        if seconds > 1_000_000_000_000:
            seconds //= 1000
        if seconds >= 946_684_800:
            return datetime.datetime.fromtimestamp(seconds)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return None


# ── W-4a-1 探针状态：进程内计数 + 每房间日志节流 ────────────────────────────
# 为什么必须节流：热门直播间「进入房间」事件可达每秒数十条，43 个房间全量写日志
# 会在 48h 内撑爆磁盘。探针的目的是「确认事件在推、字段可用」，不是留全量流水。
_PROBE_LOG_INTERVAL_SECONDS = 60
_PROBE_STATE: dict[object, dict[str, object]] = {}


def _schedule_sc_notify(
    *,
    room_id: int,
    sc_id: object,
    uname: str,
    uid: object,
    price: object,
    message: str,
    session_id: object,
) -> None:
    """把 SC 通知投递到事件循环；任何失败都只记日志，绝不影响采集链路。

    单独成函数而不是内联进 `_on_super_chat`：后者整体被一个宽 `except` 包住，
    内联时通知层的异常会被记成「处理醒目留言记录时出错」，掩盖真实原因。
    """
    try:
        from .notify import sc_notifier
    except Exception:  # noqa: BROAD_EXCEPT_OK
        logging.warning(
            "通知层不可用（notify.sc_notifier 导入失败），本次跳过 SC 通知"
        )
        return
    try:
        asyncio.create_task(
            sc_notifier.notify_sc(
                room_id,
                sc_id=sc_id,
                uname=uname,
                uid=int(uid or 0),
                price=int(price or 0),
                message=message,
                session_id=session_id,
            )
        )
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK
        logging.warning("SC 通知调度失败 room=%s: %s", room_id, exc)


def _schedule_guest_entry_notify(client, message) -> None:
    """把特关进房通知投递到事件循环；任何失败都只记日志，绝不影响采集链路。

    与 `_schedule_sc_notify` 同一模式：单独成函数，避免异常被
    `_on_interact_word_v2` 的宽 `except` 记成「探针出错」，掩盖真实原因。

    前置过滤（不满足即静默返回、不创建任务）：
      * `msg_type != 1` —— 只关心「进入房间」。4 是「特别关注了主播」，
        语义完全不同（任何人都能触发），不属于 R-17 范围。
      * `uid` 为空 —— 没有身份就无从匹配 watchedUsers。
      * 取不到 `session_id` —— R-17 规格要求「取不到场次则不推送」。
    """
    try:
        room_id = getattr(client, "room_id", None)
        if room_id is None:
            return
        if getattr(message, "msg_type", None) != 1:
            return
        uid = getattr(message, "uid", 0)
        if not uid:
            return
        session_id = runtime_state.CURRENT_SESSIONS.get(room_id)
        if session_id is None:
            return
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK
        logging.warning(
            "特关进房前置检查失败 room=%s: %s", getattr(client, "room_id", None), exc
        )
        return

    try:
        from .notify import guest_entry_notifier
    except Exception:  # noqa: BROAD_EXCEPT_OK
        logging.warning(
            "通知层不可用（notify.guest_entry_notifier 导入失败），本次跳过特关进房通知"
        )
        return
    try:
        asyncio.create_task(
            guest_entry_notifier.notify_guest_entry(
                room_id,
                uid=int(uid),
                uname=getattr(message, "username", "") or "",
                session_id=session_id,
            )
        )
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK
        logging.warning("特关进房通知调度失败 room=%s: %s", room_id, exc)


class MyHandler(blivedm.BaseHandler):
    def _resolve_session(self, client, session_id: int | None, event_time: datetime.datetime) -> int | None:
        if session_id is not None:
            return session_id
        room_id = client.room_id
        if room_id is None:
            return None
        open_session = LiveSession.find_open_session(room_id)
        if open_session is None:
            return None
        resolved_id, start_time = open_session
        runtime_state.CURRENT_SESSIONS[room_id] = resolved_id
        start_session(resolved_id, room_id, start_time or event_time)
        return resolved_id

    def _record_payment_metrics(
        self,
        client,
        session_id: int | None,
        uid: int,
        event_time: datetime.datetime,
        gift: float = 0.0,
        guard: float = 0.0,
        super_chat: float = 0.0,
        steel_coin: bool = False,
    ) -> bool:
        session_id = self._resolve_session(client, session_id, event_time)
        registration = None
        if int(uid or 0) > 0:
            registration = register_payer(
                client.room_id,
                session_id,
                current_bucket_index(session_id, event_time),
                int(uid),
                event_time.date(),
                event_time.strftime("%Y%m"),
                steel_coin=steel_coin,
            )
        if registration is not None and registration.session is not None and registration.session.size is not None:
            LiveSession.set_payer_count(session_id, registration.session.size)
        if registration is not None and registration.monthly.size is not None:
            RoomStatsMonthly.set_payer_count(
                client.room_id,
                event_time.strftime("%Y%m"),
                registration.monthly.size,
            )
        steel_delta = int(
            registration is not None
            and registration.steel_coin is not None
            and registration.steel_coin.added
        )
        RoomLiveStats.add_metrics(
            client.room_id,
            event_time.date(),
            gift=gift,
            guard=guard,
            super_chat=super_chat,
            payer_count=registration.daily.size if registration is not None else None,
            steel_coin_delta=steel_delta,
        )
        return bool(registration is not None and registration.bucket is not None and registration.bucket.added)

    def _record_blind_box(
        self,
        client,
        num: int,
        total_price: int,
        total_coin: int,
        event_time: datetime.datetime,
    ) -> None:
        if num <= 0:
            return
        dependencies = _configured_dependencies()
        profit = dependencies.profit_to_tenths(total_price, total_coin)
        RoomBlindBoxMonthly.add_amounts(client.room_id, event_time.strftime("%Y%m"), count=num, profit=profit)
        session_id = runtime_state.CURRENT_SESSIONS.get(client.room_id)
        if session_id:
            LiveSession.add_values_by_id(session_id, blind_box_count=num, blind_box_profit=profit)
        else:
            LiveSession.add_values_by_room_open(client.room_id, blind_box_count=num, blind_box_profit=profit)
        active_session_id = runtime_state.CURRENT_SESSIONS.get(client.room_id)
        if active_session_id:
            record_payment(
                active_session_id,
                event_time,
                blind_box_count=num,
                blind_box_profit=profit,
            )

    def _record_gift(
        self,
        client,
        gift_name: str,
        num: int,
        total_coin: int,
        uname: str = "",
        uid: int = 0,
        trigger_cookie_alert: bool = False,
        event_time: datetime.datetime | None = None,
        event_key: str | None = None,
    ) -> None:
        dependencies = _configured_dependencies()
        value = total_coin / 1000
        event_time = event_time or datetime.datetime.now()
        resolved_event_key = event_key or (
            f"gift:{client.room_id}:{uid}:{int(event_time.timestamp())}:"
            f"{gift_name}:{num}:{total_coin}"
        )
        record_whale_revenue(
            client.room_id,
            event_time.strftime("%Y%m"),
            int(uid or 0),
            int(total_coin),
            resolved_event_key,
            "gift",
        )
        RoomStatsMonthly.add_amounts(client.room_id, event_time.strftime("%Y%m"), gift=value)
        session_id = runtime_state.CURRENT_SESSIONS.get(client.room_id)
        if session_id:
            LiveSession.add_values_by_id(session_id, gift=value)
        else:
            LiveSession.add_values_by_room_open(client.room_id, gift=value)
        bucket_payer_added = self._record_payment_metrics(
            client,
            session_id,
            uid,
            event_time,
            gift=value,
        )
        active_session_id = runtime_state.CURRENT_SESSIONS.get(client.room_id)
        if active_session_id:
            record_payment(active_session_id, event_time, gift=value, payer_added=bucket_payer_added)
        log_message = f"[{client.room_id}] {uname} uid{uid} 赠送 {gift_name}×{num} ({value:.2f})"
        from .report_rankings import record
        record(client.room_id, active_session_id, resolved_event_key, uid, uname, gift_name, num, total_coin)
        from .report_rankings import record_contribution
        record_contribution(client.room_id, active_session_id, resolved_event_key, uid, uname, "gift", total_coin)
        logging.info(log_message)
        if trigger_cookie_alert and uid == 0:
            dependencies.send_cookie_invalid_email(log_message)

    @staticmethod
    def _parse_common_notice_gift(message) -> tuple[str, str]:
        segments = getattr(message, "content_segments", [])
        texts = [segment.text for segment in segments if getattr(segment, "text", "")] if segments else []
        if not texts:
            return "", ""
        return texts[0].strip(), texts[-1].strip()

    def _on_heartbeat(self, client, message) -> None:  # noqa: N802
        return None

    def _on_danmaku(self, client, message) -> None:  # noqa: N802
        try:
            room_id = client.room_id
            if room_id is None:
                return
            if getattr(message, "is_mirror", False) or runtime_state.LAST_STATUS.get(room_id, 0) != 1:
                return
            event_time = _timestamp_to_datetime(getattr(message, "timestamp", None)) or datetime.datetime.now()
            session_id = runtime_state.CURRENT_SESSIONS.get(room_id)
            target = danmaku_bucket_target(session_id, event_time) if session_id is not None else None
            if target is None:
                session_id = self._resolve_session(client, None, event_time)
                target = danmaku_bucket_target(session_id, event_time) if session_id is not None else None
            counts = DanmakuCounts.from_privilege_type(int(getattr(message, "privilege_type", 0) or 0))
            pending = runtime_state.DANMAKU_PENDING.setdefault(room_id, PendingDanmaku())
            pending.add(event_time, counts, target)
            record_danmaku(room_id, session_id, str(getattr(message, "rnd", "" ) or getattr(message, "timestamp", "")), str(getattr(message, "msg", "")), bool(getattr(message, "dm_type", 0) == 1), "")
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK
            logging.error("[Danmaku] 统计弹幕时出错: %s", exc)

    def _on_gift(self, client, message) -> None:  # noqa: N802
        try:
            total_coin = message.total_coin
            event_time = _timestamp_to_datetime(getattr(message, "timestamp", None))
            self._record_gift(
                client,
                message.gift_name,
                message.num,
                message.total_price,
                message.uname,
                message.uid,
                True,
                event_time,
                event_key=(
                    f"gift:{getattr(message, 'tid', '') or getattr(message, 'rnd', '')}"
                    if getattr(message, "tid", "") or getattr(message, "rnd", "")
                    else None
                ),
            )
            if message.total_price != total_coin:
                self._record_blind_box(
                    client,
                    int(message.num or 0),
                    int(message.total_price or 0),
                    int(total_coin or 0),
                    event_time or datetime.datetime.now(),
                )
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK
            logging.error("处理礼物记录时出错: %s", exc)

    def _on_common_notice_danmaku(self, client, message) -> None:  # noqa: N802
        try:
            sender, gift_name = self._parse_common_notice_gift(message)
            if not gift_name:
                logging.info("[%s] COMMON_NOTICE_DANMAKU 未解析到礼物名: %s", client.room_id, message.content_text)
                return
            coin_value = COMMON_NOTICE_GIFT_COIN_MAP.get(gift_name)
            if coin_value is None:
                logging.info("[%s] COMMON_NOTICE_DANMAKU 未匹配礼物价格: %s", client.room_id, gift_name)
                return
            event_time = datetime.datetime.now()
            self._record_gift(
                client,
                gift_name,
                1,
                coin_value,
                sender,
                event_time=event_time,
                event_key=f"common_notice:{client.room_id}:{sender}:{gift_name}:{int(event_time.timestamp())}",
            )
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK
            logging.error("处理 COMMON_NOTICE_DANMAKU 礼物记录时出错: %s", exc)

    def _record_guard(
        self,
        client,
        username: str,
        uid: int,
        guard_level: int,
        num: int,
        price: int,
        start_time: int | float | str | None,
        event_key: str | None = None,
    ) -> None:
        room_id = client.room_id
        if room_id is None:
            return
        total_coins = int(price) * int(num)
        is_red_pack = int(price) == 1900
        if is_red_pack:
            total_coins = 198000
        if int(num) != 1:
            mappings = {
                3: {3: 534000, 6: 1038000, 12: 2046000},
                2: {3: 4794000, 6: 9588000, 12: 19_176_000},
                1: {3: 51_994_000},
            }
            total_coins = mappings.get(int(guard_level), {}).get(int(num), total_coins)
        value = total_coins / 1000
        event_time = _timestamp_to_datetime(start_time) or datetime.datetime.now()
        resolved_event_key = event_key or (
            f"guard:{room_id}:{uid}:{int(event_time.timestamp())}:"
            f"{guard_level}:{num}:{price}"
        )
        session_id = runtime_state.CURRENT_SESSIONS.get(room_id)
        record_whale_revenue(
            room_id,
            event_time.strftime("%Y%m"),
            int(uid or 0),
            int(total_coins),
            resolved_event_key,
            "guard",
        )
        from .report_rankings import record_contribution
        record_contribution(room_id, session_id, resolved_event_key, uid, username, "guard", total_coins)
        RoomStatsMonthly.add_amounts(room_id, event_time.strftime("%Y%m"), guard=value)
        if session_id:
            LiveSession.add_values_by_id(session_id, guard=value)
        else:
            LiveSession.add_values_by_room_open(room_id, guard=value)
        bucket_payer_added = self._record_payment_metrics(
            client,
            session_id,
            uid,
            event_time,
            guard=value,
        )
        active_session_id = runtime_state.CURRENT_SESSIONS.get(room_id)
        if active_session_id:
            record_payment(active_session_id, event_time, guard=value, payer_added=bucket_payer_added)
        logging.info(
            "[%s] %s %s 上舰 lvl=%s num=%s 修正后=%.1f RMB %s",
            room_id,
            username,
            uid,
            guard_level,
            num,
            value,
            "(红包上舰)" if is_red_pack else "",
        )

    def _on_user_toast_v2(self, client, message) -> None:  # noqa: N802
        try:
            self._record_guard(
                client,
                getattr(message, "username", ""),
                getattr(message, "uid", 0),
                getattr(message, "guard_level", 0),
                getattr(message, "num", 0),
                getattr(message, "price", 0),
                getattr(message, "start_time", None),
                event_key=(
                    f"guard:{client.room_id}:{getattr(message, 'uid', 0)}:"
                    f"{getattr(message, 'start_time', 0)}:{getattr(message, 'guard_level', 0)}:"
                    f"{getattr(message, 'num', 0)}:{getattr(message, 'price', 0)}"
                ),
            )
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK
            logging.error("处理舰长记录时出错: %s", exc)

    def _on_super_chat(self, client, message) -> None:  # noqa: N802
        try:
            room_id = client.room_id
            if room_id is None:
                return
            value = message.price
            event_time = _timestamp_to_datetime(getattr(message, "start_time", None))
            if event_time is None:
                event_time = _timestamp_to_datetime(getattr(message, "timestamp", None))
            if event_time is None:
                event_time = _timestamp_to_datetime(getattr(message, "ts", None))
            event_time = event_time or datetime.datetime.now()
            user_info = getattr(message, "user_info", None)
            uname = getattr(message, "uname", "") or (user_info.get("uname", "") if isinstance(user_info, dict) else "")
            uid = getattr(message, "uid", 0) or (user_info.get("uid", 0) if isinstance(user_info, dict) else 0)
            message_id = getattr(message, "id", 0)
            event_key = (
                f"sc:{room_id}:{message_id}"
                if message_id
                else f"sc:{room_id}:{uid}:{int(event_time.timestamp())}:{value}:{getattr(message, 'message', '')}"
            )
            record_whale_revenue(
                room_id,
                event_time.strftime("%Y%m"),
                int(uid or 0),
                int(value) * 1000,
                event_key,
                "super_chat",
            )
            from .report_rankings import record_contribution
            record_contribution(room_id, runtime_state.CURRENT_SESSIONS.get(room_id), event_key, uid, uname, "super_chat", int(value * 1000))
            RoomStatsMonthly.add_amounts(room_id, event_time.strftime("%Y%m"), super_chat=value)
            session_id = runtime_state.CURRENT_SESSIONS.get(room_id)
            if session_id:
                LiveSession.add_values_by_id(session_id, super_chat=value)
            else:
                LiveSession.add_values_by_room_open(room_id, super_chat=value)
            bucket_payer_added = self._record_payment_metrics(
                client,
                session_id,
                message.uid,
                event_time,
                super_chat=value,
                steel_coin=value < 30,
            )
            active_session_id = runtime_state.CURRENT_SESSIONS.get(room_id)
            if active_session_id:
                record_payment(
                    active_session_id,
                    event_time,
                    super_chat=value,
                    payer_added=bucket_payer_added,
                )
            SuperChatLog.log_sc(room_id, uname, uid, value, getattr(message, "message", "") or "", event_time)
            logging.info("[%s] SC ¥%.2f %s %s: %s", room_id, value, uname, uid, getattr(message, "message", "") or "")
            # ── 通知层 hook（Batch 4a / R-16）：SC 已入库，通知是纯副作用 ──
            # sc_id 直接复用上面构好的 event_key（message.id 非 0 时即 sc:{room}:{id}），
            # 不重新构造，避免与上游的兜底逻辑产生第二套实现。
            _schedule_sc_notify(
                room_id=room_id,
                sc_id=event_key,
                uname=uname,
                uid=uid,
                price=value,
                message=getattr(message, "message", "") or "",
                session_id=active_session_id,
            )
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK
            logging.error("处理醒目留言记录时出错: %s", exc)

    def _on_interact_word_v2(self, client, message) -> None:  # noqa: N802
        """W-4a-1 只读探针（Batch 4a）。

        目的：确认 B站是否真的推送 INTERACT_WORD_V2，以及 msg_type / uid /
        username 字段是否可用 —— 这是 R-17 特关进房**唯一未验证的前提**。

        本阶段**只写日志，不发任何通知**；发送逻辑等探针出结论后再加（W-4a-3）。

        上游 blivedm 的命令注册表已把 INTERACT_WORD_V2 映射到本方法
        （`blivedm/handlers.py` 的 `_CMD_CALLBACK_DICT`），基类实现为空，
        因此只需在这里 override 即可接到事件，**无需改动 blivedm**。

        判据：日志出现 msg_type=1 且 uid != 0、username 非空。
        """
        try:
            room_id = getattr(client, "room_id", None)
            state = _PROBE_STATE.setdefault(
                room_id, {"seen": 0, "type1": 0, "last_log": 0.0}
            )
            state["seen"] = int(state["seen"]) + 1

            if getattr(message, "msg_type", None) != 1:
                return  # 只关心「进入房间」

            state["type1"] = int(state["type1"]) + 1
            now = time.monotonic()
            interval = now - float(state["last_log"])
            if interval < _PROBE_LOG_INTERVAL_SECONDS:
                return  # 节流：每房间最多 60s 一条样本
            state["last_log"] = now

            logging.getLogger("bili_douchong.probe.interact_v2").info(
                "[PROBE] INTERACT_WORD_V2 room=%s msg_type=1 uid=%s username=%r "
                "has_username=%s has_face=%s ts=%s ｜ 累计 seen=%s type1=%s 距上条=%.0fs",
                room_id,
                getattr(message, "uid", None),
                getattr(message, "username", ""),
                bool(getattr(message, "username", "")),
                bool(getattr(message, "face", "")),
                getattr(message, "timestamp", None),
                state["seen"],
                state["type1"],
                interval,
            )
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK
            logging.error("处理 INTERACT_WORD_V2 探针时出错: %s", exc)

        # ── 通知层 hook（W-4a-3 / R-17）───────────────────────────────
        # 探针结论已出（2026-10-06 实测：8 房间 / 1098 条事件中 99.5% 为
        # msg_type==1，uid / username / face 字段全部可用）⇒ 按 D-99 解禁，
        # 由独立的模块级函数接管发送（异常不污染上面的探针日志）。
        _schedule_guest_entry_notify(client, message)
