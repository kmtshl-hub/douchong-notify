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
#   upstream file: app/notifications.py
#
from __future__ import annotations
# live/liveEnd 已由通知层接管；D-84 的落点在 /app/app/notify/。
import asyncio, datetime, logging, os
from zoneinfo import ZoneInfo
import aiohttp
from . import bilibili_gateway, room_config, runtime_state
from .notify import dedup

LOG = logging.getLogger("bili_douchong.notifications")
ENABLED = os.getenv("BILI_NOTIFY_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
INITIAL_SILENT = os.getenv("BILI_NOTIFY_INITIAL_SILENT", "1").lower() in {"1", "true", "yes", "on"}
GROUP_IDS = tuple(int(v.strip()) for v in os.getenv("BILI_NOTIFY_GROUPS", "").split(",") if v.strip().isdigit())
HTTP_URL = os.getenv("ONEBOT_HTTP_URL", "http://127.0.0.1:13000").rstrip("/")
HTTP_TOKEN = os.getenv("ONEBOT_HTTP_TOKEN", "")

NOTIFIED_KEY = "qqnotify:live-notified"
NOTIFIED_TTL_SECONDS = 86400
_prewarm_notified: set[str] = set()
_prewarm_live_time: dict[int, str] = {}
_prewarm_valid: bool = False

_seen: set[int] = set(); _active: set[int] = set()
# 开播时记下 session_id，供下播通知使用。
# 为什么需要：room_lifecycle.finish_live_session() 是**先** CURRENT_SESSIONS.pop(room_id)
# **再**调 schedule_live_off()，所以下播时直接查 CURRENT_SESSIONS 必然是 None
# —— 那会让下播通知每次都失去 Redis 去重，并每次打一条 warn 噪音（决策 D-85）。
_live_session_ids: dict[int, int] = {}
SHANGHAI = ZoneInfo("Asia/Shanghai")

def _live_notifier():
    """惰性拿到通知层模块；不可用则返回 None（绝不抛异常）。"""
    try:
        from . import notify as _notify_pkg
        from .notify import live_notifier as _live_notifier_module
        return _live_notifier_module
    except Exception:
        LOG.warning("通知层不可用（notify.live_notifier 导入失败），本次跳过斗虫侧通知")
        return None

def _remember_session(room_id: int) -> int | None:
    """开播时记录 session_id；不做任何失败处理（取不到就返回 None）。"""
    try:
        session_id = runtime_state.CURRENT_SESSIONS.get(room_id)
    except Exception:  # noqa: BROAD_EXCEPT_OK
        return None
    if session_id is not None:
        _live_session_ids[int(room_id)] = session_id
    return session_id


def _fingerprint(live_time_raw) -> str:
    """场次指纹：与上游 monitoring_jobs.py 完全相同的转换式，保证与 LIVE_INFO 可比。"""
    try:
        return datetime.datetime.fromtimestamp(int(live_time_raw)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


async def restore_live_state() -> None:
    """启动预热：读回已通知指纹并查询当前在播场次；任一步失败都回退上游语义。"""
    global _prewarm_valid
    _prewarm_valid = False
    _prewarm_notified.clear()
    _prewarm_live_time.clear()
    try:
        client = await dedup._get_redis()
        notified = await client.smembers(NOTIFIED_KEY)

        session = runtime_state.aiohttp_session
        if session is None or not runtime_state.ROOM_UIDS:
            LOG.warning("[prewarm] session 或 ROOM_UIDS 未就绪，跳过")
            return
        params = [("uids[]", str(uid)) for uid in runtime_state.ROOM_UIDS.values()]
        async with session.get(
            bilibili_gateway.LIVE_STATUS_API,
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={
                "User-Agent": bilibili_gateway.USER_AGENT,
                "Referer": "https://live.bilibili.com",
            },
        ) as response:
            if response.status != 200:
                LOG.warning("[prewarm] 状态查询 HTTP %s", response.status)
                return
            payload = await response.json(content_type=None)

        data = payload.get("data") or {}
        live_times: dict[int, str] = {}
        for room_id, uid in tuple(runtime_state.ROOM_UIDS.items()):
            info = data.get(str(uid)) or {}
            if "live_status" not in info:
                raise ValueError(f"missing live_status for uid={uid}")
            status = int(info.get("live_status") or 0)
            status = 0 if status == 2 else status
            if status != 1:
                continue
            fingerprint = _fingerprint(info.get("live_time"))
            if fingerprint:
                live_times[int(room_id)] = fingerprint

        _prewarm_notified.update(str(value) for value in notified)
        _prewarm_live_time.update(live_times)
        _prewarm_valid = True
        LOG.info(
            "[prewarm] 预热成功：已通知指纹 %d 条，在播房间 %d 个",
            len(_prewarm_notified),
            len(_prewarm_live_time),
        )
    except Exception as exc:
        _prewarm_notified.clear()
        _prewarm_live_time.clear()
        _prewarm_valid = False
        LOG.warning("[prewarm] 预热失败，回退上游原语义: %s", type(exc).__name__)


def startup_live_rooms() -> set[int]:
    """返回「启动预热时处于在播」的房间集合；预热失败时为空集（调用方应回退原顺序）。"""
    return set(_prewarm_live_time)


async def _mark_notified(room_id: int) -> None:
    """把本场次已通知标记写入 Redis；失败不抛异常、不阻塞通知发送。"""
    try:
        fingerprint = runtime_state.LIVE_INFO.get(int(room_id), {}).get("live_time", "")
        if not fingerprint:
            return
        client = await dedup._get_redis()
        pipe = client.pipeline()
        pipe.sadd(NOTIFIED_KEY, f"{int(room_id)}:{fingerprint}")
        pipe.expire(NOTIFIED_KEY, NOTIFIED_TTL_SECONDS)
        await pipe.execute()
    except Exception as exc:
        LOG.warning("[notified] 写入失败（忽略）: %s", type(exc).__name__)


def observe(room_id: int, status: int) -> bool:
    """返回是否应按“监控前就已在播”静默。"""
    room_id = int(room_id); first = room_id not in _seen; _seen.add(room_id)
    if int(status) == 1: _active.add(room_id)
    if not _prewarm_valid:
        return first
    if not first:
        return False
    fingerprint = _prewarm_live_time.get(room_id)
    if not fingerprint:
        return False
    return f"{room_id}:{fingerprint}" in _prewarm_notified

def _label(room_id: int) -> str: return room_config.get_room_anchor_name(room_id) or str(room_id)

def _duration(room_id: int) -> str:
    start = runtime_state.STREAM_STARTS.get(room_id)
    if start is None: return "未知"
    if hasattr(start, "timestamp"): seconds = max(0, int(datetime.datetime.now(SHANGHAI).timestamp() - start.timestamp()))
    else: seconds = max(0, int(datetime.datetime.now(SHANGHAI).timestamp() - float(start)))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}小时{rest // 60}分钟" if hours else f"{rest // 60}分钟"

async def _info(room_id: int) -> dict[str, str]:
    session = runtime_state.aiohttp_session
    if session is None: return {}
    try:
        async with session.get("https://api.live.bilibili.com/room/v1/Room/get_info", params={"room_id": room_id}, headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://live.bilibili.com/{room_id}"}, timeout=aiohttp.ClientTimeout(total=5)) as response:
            payload = await response.json(content_type=None)
            data = payload.get("data") or {}
        return {"title": str(data.get("title") or ""), "cover": str(data.get("user_cover") or data.get("cover") or ""), "area": " / ".join(str(x) for x in (data.get("parent_area_name"), data.get("area_name")) if x), "anchor": str(data.get("anchor_name") or "")}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
        LOG.exception("room info fetch failed room=%s", room_id); return {}

async def _send(group_id: int, rich: list[dict], fallback: str) -> None:
    session = runtime_state.aiohttp_session
    if not ENABLED or not GROUP_IDS or session is None: return
    headers = {"Authorization": f"Bearer {HTTP_TOKEN}"} if HTTP_TOKEN else {}
    async with session.post(f"{HTTP_URL}/send_group_msg", json={"group_id": group_id, "message": rich}, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
        payload = await response.json(content_type=None)
        ok = response.status < 400 and payload.get("retcode", 0) == 0 and payload.get("status", "ok") == "ok"
    if not ok:
        async with session.post(f"{HTTP_URL}/send_group_msg", json={"group_id": group_id, "message": [{"type": "text", "data": {"text": fallback}}]}, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
            if response.status >= 400: LOG.warning("notification fallback failed group=%s", group_id)

async def _on(room_id: int, initial: bool) -> None:
    if initial and INITIAL_SILENT: return
    info = await _info(room_id)
    live_info = runtime_state.LIVE_INFO.get(room_id, {})
    info.setdefault("title", live_info.get("title", ""))
    name = info.get("anchor") or _label(room_id); url = f"https://live.bilibili.com/{room_id}"
    text = f"【开播通知】\n{name} 开播啦~\n直播标题：{info.get('title', '')}\n直播分区：{info.get('area') or '未知'}\n开播时间：{datetime.datetime.now(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')}\n直播间：{url}"
    cover = info.get("cover", ""); rich = [{"type": "text", "data": {"text": text}}]
    if cover: rich.append({"type": "image", "data": {"file": cover}})
    notifier = _live_notifier()
    if notifier is None: return
    result = await notifier.notify_live(room_id, data={"name": info.get("anchor") or _label(room_id), "title": info.get("title", ""), "area": info.get("area") or "未知", "cover": cover, "time_text": datetime.datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")}, initial=initial, session_id=_live_session_ids.get(room_id) or runtime_state.CURRENT_SESSIONS.get(room_id))
    if isinstance(result, dict) and (result.get("sent") is True or result.get("skipped") == "duplicate"):
        await _mark_notified(room_id)

def schedule_live_on(room_id: int, title: str = "", initial: bool = False) -> None:
    room_id = int(room_id); _seen.add(room_id); _active.add(room_id); _remember_session(room_id); asyncio.create_task(_on(room_id, initial))

def schedule_live_off(room_id: int, duration: str | None = None) -> None:
    room_id = int(room_id)
    if room_id not in _active: return
    _active.discard(room_id); text = f"【下播通知】\n{_label(room_id)} 下播了\n本场时长：{duration or _duration(room_id)}"
    async def send() -> None:
        notifier = _live_notifier()
        if notifier is None: return
        # 下播时 CURRENT_SESSIONS 已被 pop，所以先取开播时记住的那个；两者都无则保持 None。
        session_id = _live_session_ids.pop(room_id, None)
        if session_id is None:
            session_id = runtime_state.CURRENT_SESSIONS.get(room_id)
        await notifier.notify_live_off(room_id, data={"name": _label(room_id), "duration_text": duration or _duration(room_id)}, session_id=session_id)
    asyncio.create_task(send())
