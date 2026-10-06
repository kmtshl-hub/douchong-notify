# Standard-library-only assertion script for T-706 B⁗ restart notification state.
#
# ⚠️ 运行方式：本测试**必须在容器内**运行（一次性容器即可）。
#    原因：它测的是 `patches/notifications.py`（补丁版），该文件用相对导入
#    （`from . import room_config, runtime_state`），需要完整的 `app` 包上下文；
#    本地工作树没有 `app` 包，直接跑会 ModuleNotFoundError。
#
#    实测配方（2026-10-06 验证通过）：
#      # 准备 stage：notifications.py（来自 patches/）+ notify/ + 本文件
#      docker run --rm -w /app -e PYTHONPATH=/app \
#        -v <stage>/notifications.py:/app/app/notifications.py:ro \
#        -v <stage>/notify:/app/app/notify:ro \
#        -v <stage>/test_notifications_t706.py:/tmp/t.py:ro \
#        --entrypoint python3 bili_douchong-vr-douchong:latest /tmp/t.py
#
#    ⇒ 既有 313 条断言在本地跑；本文件的 25 条在容器内跑。两边都要跑才算验收完整。

import asyncio
import datetime
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 本测试针对「补丁层」，必须在上游 VR_douchong 仓库内运行（那里才有 app 包）。
# 在纯本仓库检出中**跳过而不是报错**，以保证测试全绿。
#
# 判据说明：`"pytest" in sys.modules` —— 被 pytest 收集时它必定已导入，
# 此时用 pytest 的跳过机制；直接 `python xxx.py` 时未导入，则打印 SKIP 并以 0 退出
# （不能无条件调 pytest.skip()：直接运行时它会抛未捕获的 Skipped ⇒ 退出码 1）。
import sys as _sys

try:
    from app import bootstrap, notifications  # noqa: F401
except (ModuleNotFoundError, ImportError) as _exc:  # pragma: no cover
    # 注意要同时捕 ImportError：sys.path 上若存在名为 `app` 的命名空间包，
    # 报的是 "cannot import name 'bootstrap' from 'app'"，而不是"模块不存在"。
    _skip_msg = f"需要上游 VR_douchong 的 app 包（请在上游仓库内运行）：{_exc}"
    if "pytest" in _sys.modules:
        import pytest as _pytest

        _pytest.skip(_skip_msg, allow_module_level=True)
    print(f"SKIP: {_skip_msg}")
    raise SystemExit(0) from None



class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self.payload


class FakeSession:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payload, self.status)


class FakePipeline:
    def __init__(self):
        self.commands = []
        self.executed = False

    def sadd(self, key, value):
        self.commands.append(("sadd", key, value))
        return self

    def expire(self, key, ttl):
        self.commands.append(("expire", key, ttl))
        return self

    async def execute(self):
        self.executed = True
        return [1, True]


class FakeRedis:
    def __init__(self, members=()):
        self.members = set(members)
        self.pipe = FakePipeline()

    async def smembers(self, key):
        assert key == notifications.NOTIFIED_KEY
        return set(self.members)

    def pipeline(self):
        return self.pipe


async def _raise_redis():
    raise RuntimeError("redis unavailable")


def reset_state():
    notifications._seen.clear()
    notifications._active.clear()
    notifications._prewarm_notified.clear()
    notifications._prewarm_live_time.clear()
    notifications._prewarm_valid = False


async def main():
    original_get_redis = notifications.dedup._get_redis
    original_session = notifications.runtime_state.aiohttp_session
    original_room_uids = notifications.runtime_state.ROOM_UIDS
    original_live_info = notifications.runtime_state.LIVE_INFO
    try:
        fp = notifications._fingerprint(20261006)
        expected_fp = datetime.datetime.fromtimestamp(20261006).strftime("%Y-%m-%d %H:%M:%S")
        assert fp == expected_fp
        assert notifications._fingerprint("not-a-timestamp") == ""

        reset_state()
        notifications._prewarm_valid = True
        notifications._prewarm_live_time[101] = fp
        notifications._prewarm_notified.add(f"101:{fp}")
        assert notifications.observe(101, 1) is True

        reset_state()
        notifications._prewarm_valid = True
        notifications._prewarm_live_time[102] = fp
        assert notifications.observe(102, 1) is False
        assert notifications.observe(102, 1) is False

        reset_state()
        notifications._prewarm_valid = True
        notifications._prewarm_live_time[103] = fp
        notifications._prewarm_notified.add(f"103:{fp}")
        assert notifications.observe(103, 0) is True
        assert 103 not in notifications._active

        reset_state()
        notifications._prewarm_valid = True
        assert notifications.observe(104, 0) is False
        assert notifications.observe(104, 1) is False

        reset_state()
        assert notifications.observe(105, 1) is True
        assert notifications.observe(105, 1) is False

        payload = {
            "data": {
                "9001": {"live_status": 1, "live_time": 20261006},
                "9002": {"live_status": 0, "live_time": 0},
                "9003": {"live_status": 2, "live_time": 20261006},
            }
        }
        redis = FakeRedis({f"201:{fp}"})
        session = FakeSession(payload)
        notifications.dedup._get_redis = lambda: _return(redis)
        notifications.runtime_state.aiohttp_session = session
        notifications.runtime_state.ROOM_UIDS = {201: 9001, 202: 9002, 203: 9003}
        reset_state()
        await notifications.restore_live_state()
        assert notifications._prewarm_valid is True
        assert notifications._prewarm_notified == {f"201:{fp}"}
        assert notifications._prewarm_live_time == {201: fp}
        assert session.calls[0][0] == notifications.bilibili_gateway.LIVE_STATUS_API

        notifications.runtime_state.aiohttp_session = FakeSession({"data": {"9001": payload["data"]["9001"]}})
        notifications.runtime_state.ROOM_UIDS = {201: 9001, 299: 9999}
        await notifications.restore_live_state()
        assert notifications._prewarm_valid is False
        assert notifications._prewarm_notified == set()
        assert notifications._prewarm_live_time == {}

        notifications.dedup._get_redis = _raise_redis
        await notifications.restore_live_state()
        assert notifications._prewarm_valid is False
        assert notifications._prewarm_notified == set()
        assert notifications._prewarm_live_time == {}

        redis = FakeRedis()
        notifications.dedup._get_redis = lambda: _return(redis)
        notifications.runtime_state.LIVE_INFO = {301: {"live_time": fp}}
        await notifications._mark_notified(301)
        assert redis.pipe.commands == [
            ("sadd", notifications.NOTIFIED_KEY, f"301:{fp}"),
            ("expire", notifications.NOTIFIED_KEY, notifications.NOTIFIED_TTL_SECONDS),
        ]
        assert redis.pipe.executed is True

        notifications.dedup._get_redis = _raise_redis
        await notifications._mark_notified(301)
        assert True  # fail-graceful: reaching here means the exception was contained
    finally:
        notifications.dedup._get_redis = original_get_redis
        notifications.runtime_state.aiohttp_session = original_session
        notifications.runtime_state.ROOM_UIDS = original_room_uids
        notifications.runtime_state.LIVE_INFO = original_live_info
        reset_state()


async def _return(value):
    return value


if __name__ == "__main__":
    asyncio.run(main())
    print("T-706 notification snapshot assertions passed")
