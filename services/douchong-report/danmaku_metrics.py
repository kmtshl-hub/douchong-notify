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
#   upstream file: app/danmaku_metrics.py
#
"""Short-lived danmaku word/emote aggregates; no message text is persisted."""
import re, time, logging
import redis
from .config import REDIS_URL

client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
TTL = 7 * 86400
WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_]{1,}")

def record(room: int, session: int | None, event_id: str, text: str, is_emote: bool = False, emote_name: str = "") -> None:
    if not session: return
    prefix=f"qqreport:danmaku:v1:{room}:{session}"
    try:
        if client.sadd(prefix+":dedupe", event_id) == 0: return
        client.expire(prefix+":dedupe", TTL)
        client.set(f"qqreport:danmaku:v1:{room}:latest", session, ex=TTL)
        if is_emote or emote_name:
            name=emote_name or text or "未知表情"
            client.zincrby(prefix+":emotes", 1, name); client.expire(prefix+":emotes", TTL)
            return
        for word in WORD_RE.findall(text or ""):
            client.zincrby(prefix+":words", 1, word); client.expire(prefix+":words", TTL)
    except redis.RedisError:
        logging.warning("danmaku metrics unavailable room=%s", room)

def read(room: int, session: int | None = None) -> dict:
    try:
        session = session or client.get(f"qqreport:danmaku:v1:{room}:latest")
        if not session: return {"available": False, "reason": "尚无短期弹幕统计"}
        p=f"qqreport:danmaku:v1:{room}:{session}"
        return {"available": True, "session_id": int(session), "retention_days": 7,
                "words":[{"word":w,"count":int(c)} for w,c in client.zrevrange(p+":words",0,49,withscores=True)],
                "emotes":[{"name":w,"count":int(c)} for w,c in client.zrevrange(p+":emotes",0,49,withscores=True)]}
    except redis.RedisError:
        return {"available": False, "reason": "短期弹幕统计暂不可用"}
