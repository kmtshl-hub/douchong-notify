# Standard-library-only assertion script for notify.dedup.

import asyncio
import logging
import pathlib
import sys
from unittest import mock

logging.getLogger("bili_douchong.notify").disabled = True

SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from notify import dedup, sub_client


class FakeRedis:
    def __init__(self, fail=False):
        self.fail = fail
        self.keys = set()
        self.calls = []

    async def set(self, key, value, *, nx, ex):
        self.calls.append(
            {"key": key, "value": value, "nx": nx, "ex": ex}
        )
        if self.fail:
            raise RuntimeError("redis unavailable")
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True

    async def aclose(self):
        return None


def main():
    count = 0
    original_client = dedup._redis_client

    try:
        fake = FakeRedis()
        dedup._redis_client = fake
        first = asyncio.run(
            dedup.first_seen("live", 700000001, 12345, 60)
        )
        second = asyncio.run(
            dedup.first_seen("live", 700000001, 12345, 60)
        )
        assert first is True, "同键第一次必须返回 True"
        count += 1
        assert second is False, "同键第二次必须返回 False"
        count += 1

        assert fake.calls[0]["nx"] is True, "Redis SET 必须使用 nx=True"
        count += 1
        assert fake.calls[0]["ex"] == 60, "Redis SET 必须传 ex=ttl_seconds"
        count += 1

        failing = FakeRedis(fail=True)
        dedup._redis_client = failing
        with mock.patch.object(dedup.LOGGER, "warning") as warning:
            fail_open = asyncio.run(
                dedup.first_seen("sc", 700000001, "sc-1", 300)
            )
        assert fail_open is True, "Redis 异常必须 fail open 返回 True"
        count += 1
        assert warning.call_count >= 1, "Redis 异常必须记录 warn 日志"
        count += 1

        key_from_dedup = dedup.dedup_key(
            "guestEntry", 700000001, "800000001:98765"
        )
        key_from_sub_client = sub_client.dedup_key(
            "guestEntry", 700000001, "800000001:98765"
        )
        assert (
            key_from_dedup == key_from_sub_client
        ), "dedup.py 必须复用 sub_client 的键格式"
        count += 1
        assert (
            key_from_dedup
            == "qqnotify:dedup:guestEntry:700000001:800000001:98765"
        ), "去重键逐字格式不符"
        count += 1

    except AssertionError as exc:
        print(f"FAIL dedup: {exc}")
        return 1
    except Exception as exc:
        print(f"FAIL dedup: 未预期异常 {type(exc).__name__}: {exc}")
        return 1
    finally:
        dedup._redis_client = original_client

    print(f"OK dedup {count} 条断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
