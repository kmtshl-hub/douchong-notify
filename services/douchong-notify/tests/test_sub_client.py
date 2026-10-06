
# Standard-library-only assertion script for notify.sub_client.

import asyncio
import logging
import pathlib
import sys

logging.getLogger("bili_douchong.notify").disabled = True

SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from notify import sub_client


class FakeResponse:
    def __init__(self, status, payload=None, json_error=None):
        self.status = status
        self._payload = payload
        self._json_error = json_error

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeRequestContext:
    def __init__(self, item):
        self._item = item

    async def __aenter__(self):
        if isinstance(self._item, BaseException):
            raise self._item
        return self._item

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, items):
        self.items = list(items)
        self.calls = []
        self.closed = False

    def get(self, url, *, headers, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        if not self.items:
            raise AssertionError("FakeSession 没有剩余响应")
        return FakeRequestContext(self.items.pop(0))

    async def close(self):
        self.closed = True


def _snapshot(groups=None):
    return {
        "version": 2,
        "groups": {} if groups is None else groups,
    }


def main():
    count = 0
    original_token = sub_client.NOTIFY_API_TOKEN
    original_session = sub_client._session
    original_get_snapshot = sub_client.get_snapshot

    try:
        valid = _snapshot(
            {"900000001": {"anchors": {}}}
        )
        assert sub_client.parse_snapshot(valid) is valid, (
            "合法 payload 必须原样返回"
        )
        count += 1
        assert sub_client.parse_snapshot(
            {"version": 1, "groups": {}}
        ) is None, "v1 必须拒绝"
        count += 1
        assert sub_client.parse_snapshot(
            {"version": 3, "groups": {}}
        ) is None, "v3 必须拒绝"
        count += 1

        assert sub_client.parse_snapshot([]) is None, (
            "非 dict 必须拒绝"
        )
        count += 1
        assert sub_client.parse_snapshot(
            {"version": 2, "groups": []}
        ) is None, "groups 非 dict 必须拒绝"
        count += 1
        assert sub_client.parse_snapshot(
            {"version": "2", "groups": {}}
        ) is None, "字符串版本号必须拒绝"
        count += 1

        strict_groups = {
            "900000001": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": True}
                    }
                }
            },
            "900000002": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": "true"}
                    }
                }
            },
            "900000003": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": 1}
                    }
                }
            },
            "900000004": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": 0}
                    }
                }
            },
            "900000005": {
                "anchors": {
                    "700000001": {
                        "notify": {}
                    }
                }
            },
        }
        assert sub_client.select_targets(
            _snapshot(strict_groups),
            700000001,
            "live",
        ) == [900000001], "只有 is True 才算开启"
        count += 1

        sorted_groups = {
            "900000003": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": True}
                    }
                }
            },
            "900000001": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": True}
                    }
                }
            },
            "900000002": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": True}
                    }
                }
            },
        }
        sorted_result = sub_client.select_targets(
            _snapshot(sorted_groups),
            700000001,
            "live",
        )
        assert sorted_result == [
            900000001,
            900000002,
            900000003,
        ], "多群结果必须排序"
        count += 1
        assert all(
            type(group_id) is int
            for group_id in sorted_result
        ), "返回群号必须是 int"
        count += 1

        string_room_groups = {
            "900000001": {
                "anchors": {
                    "700000001": {
                        "notify": {"liveEnd": True}
                    }
                }
            }
        }
        assert sub_client.select_targets(
            _snapshot(string_room_groups),
            700000001,
            "liveEnd",
        ) == [900000001], "房间键必须按 str(room_id) 查"
        count += 1

        invalid_group_id = {
            "abc": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": True}
                    }
                }
            },
            "900000001": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": True}
                    }
                }
            },
        }
        assert sub_client.select_targets(
            _snapshot(invalid_group_id),
            700000001,
            "live",
        ) == [900000001], "非法群号必须跳过"
        count += 1

        assert sub_client.select_targets(
            _snapshot(sorted_groups),
            700000001,
            "guestEntry",
        ) == [], "非白名单事件必须返回空列表"
        count += 1

        # ★ 让"是否排序"真正可测：
        # {900000001, 900000008} 的 Python set 迭代顺序恰为降序。
        descending_set_order = {
            "900000008": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": True}
                    }
                }
            },
            "900000001": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": True}
                    }
                }
            },
        }
        assert sub_client.select_targets(
            _snapshot(descending_set_order),
            700000001,
            "live",
        ) == [
            900000001,
            900000008,
        ], "结果必须升序排序（不能直接交出 set 的迭代顺序）"
        count += 1

        detailed_groups = {
            "900000008": {
                "anchors": {
                    "700000001": {
                        "notify": {
                            "live": True,
                            "atAll": True,
                        }
                    }
                }
            },
            "900000001": {
                "anchors": {
                    "700000001": {
                        "notify": {
                            "live": True,
                            "atAll": "true",
                        }
                    }
                }
            },
            "900000003": {
                "anchors": {
                    "700000001": {
                        "notify": {
                            "live": True,
                            "atAll": 1,
                        }
                    }
                }
            },
            "900000002": {
                "anchors": {
                    "700000001": {
                        "notify": {"live": True}
                    }
                }
            },
            "bad-group": {
                "anchors": {
                    "700000001": {
                        "notify": {
                            "live": True,
                            "atAll": True,
                        }
                    }
                }
            },
        }
        detailed = sub_client.select_targets_detailed(
            _snapshot(detailed_groups),
            700000001,
            "live",
        )
        assert detailed == [
            {
                "group_id": 900000001,
                "at_all": False,
            },
            {
                "group_id": 900000002,
                "at_all": False,
            },
            {
                "group_id": 900000003,
                "at_all": False,
            },
            {
                "group_id": 900000008,
                "at_all": True,
            },
        ], (
            "detailed 必须含 group_id/at_all、升序，"
            "且 atAll 仅严格 True 生效"
        )
        count += 1

        assert [
            item["group_id"]
            for item in detailed
        ] == sub_client.select_targets(
            _snapshot(detailed_groups),
            700000001,
            "live",
        ), "detailed 与 select_targets 的群号必须逐项一致"
        count += 1

        assert sub_client.select_targets_detailed(
            _snapshot(detailed_groups),
            700000001,
            "guestEntry",
        ) == [], "detailed 必须沿用事件白名单"
        count += 1

        guest_groups = {
            "900000001": {
                "guestEntryEnabled": True,
                "watchedUsers": [
                    {
                        "uid": "800000001",
                        "name": "A",
                    }
                ],
            },
            "900000002": {
                "guestEntryEnabled": False,
                "watchedUsers": [
                    {
                        "uid": "800000001",
                        "name": "B",
                    }
                ],
            },
            "900000003": {
                "guestEntryEnabled": True,
                "watchedUsers": [
                    {
                        "uid": "800000002",
                        "name": "C",
                    }
                ],
            },
        }
        assert sub_client.select_guest_targets(
            _snapshot(guest_groups),
            "800000001",
        ) == [900000001], (
            "特关必须同时满足群开关与 uid 命中"
        )
        count += 1

        numeric_uid_groups = {
            "900000001": {
                "guestEntryEnabled": True,
                "watchedUsers": [
                    {
                        "uid": 800000001,
                        "name": "A",
                    }
                ],
            }
        }
        assert sub_client.select_guest_targets(
            _snapshot(numeric_uid_groups),
            "800000001",
        ) == [900000001], "uid 必须按字符串比较"
        count += 1

        assert (
            sub_client.dedup_key(
                "live",
                700000001,
                12345,
            )
            == "qqnotify:dedup:live:700000001:12345"
        ), "live 去重键逐字不符"
        count += 1

        assert (
            sub_client.dedup_key(
                "guestEntry",
                700000001,
                "800000001:98765",
            )
            == (
                "qqnotify:dedup:guestEntry:"
                "700000001:800000001:98765"
            )
        ), "guestEntry 五段去重键逐字不符"
        count += 1

        async def cache_case():
            sub_client.reset_cache()
            sub_client.NOTIFY_API_TOKEN = "test-token"
            fake = FakeSession(
                [FakeResponse(200, _snapshot())]
            )
            sub_client._session = fake
            first = await sub_client.get_snapshot()
            second = await sub_client.get_snapshot()
            return first, second, fake

        first, second, cache_session = asyncio.run(
            cache_case()
        )
        assert (
            first == _snapshot()
            and second == _snapshot()
        ), "缓存场景必须返回成功快照"
        count += 1
        assert len(cache_session.calls) == 1, (
            "第二次缓存命中不得产生新请求"
        )
        count += 1

        async def stale_case():
            sub_client.reset_cache()
            sub_client.NOTIFY_API_TOKEN = "test-token"
            stale = _snapshot(
                {"900000001": {"anchors": {}}}
            )
            fake = FakeSession(
                [
                    FakeResponse(200, stale),
                    RuntimeError("network down"),
                ]
            )
            sub_client._session = fake
            first_value = await sub_client.get_snapshot(
                force=True
            )
            second_value = await sub_client.get_snapshot(
                force=True
            )
            return stale, first_value, second_value, fake

        (
            stale,
            first_value,
            second_value,
            stale_session,
        ) = asyncio.run(stale_case())

        assert first_value is stale, (
            "第一次成功必须缓存原快照"
        )
        count += 1
        assert second_value is stale, (
            "300 秒内请求失败必须返回上一次成功快照"
        )
        count += 1
        assert len(stale_session.calls) == 2, (
            "force=True 必须真的发起第二次请求"
        )
        count += 1

        # ★ 旧快照必须有"保质期"：把上次成功时间手工推到 301 秒前。
        async def stale_expired_case():
            sub_client.reset_cache()
            sub_client.NOTIFY_API_TOKEN = "test-token"
            snapshot_value = _snapshot(
                {"900000001": {"anchors": {}}}
            )
            fake = FakeSession(
                [
                    FakeResponse(200, snapshot_value),
                    RuntimeError("network down"),
                ]
            )
            sub_client._session = fake
            fresh = await sub_client.get_snapshot(
                force=True
            )
            sub_client._cache_success_at -= (
                sub_client.STALE_LIMIT_SECONDS + 1
            )
            expired = await sub_client.get_snapshot(
                force=True
            )
            return fresh, expired

        fresh_value, expired_value = asyncio.run(
            stale_expired_case()
        )
        assert fresh_value is not None, (
            "第一次必须成功拿到快照（前置条件）"
        )
        count += 1
        assert expired_value is None, (
            "旧快照超过 STALE_LIMIT_SECONDS(=300) 秒后"
            "必须返回 None，不得无限期复用"
        )
        count += 1

        async def unavailable_snapshot(*, force=False):
            return None

        sub_client.get_snapshot = unavailable_snapshot
        unavailable_targets = asyncio.run(
            sub_client.targets_for(
                700000001,
                "live",
            )
        )
        assert unavailable_targets is None, (
            "订阅不可用时 targets_for 必须返回 None"
        )
        count += 1

        unavailable_detailed = asyncio.run(
            sub_client.targets_detailed(
                700000001,
                "live",
            )
        )
        assert unavailable_detailed is None, (
            "订阅不可用时 targets_detailed 必须返回 None"
        )
        count += 1

        async def empty_snapshot(*, force=False):
            return _snapshot(
                {"900000001": {"anchors": {}}}
            )

        sub_client.get_snapshot = empty_snapshot
        empty_targets = asyncio.run(
            sub_client.targets_for(
                700000001,
                "live",
            )
        )
        assert empty_targets == [], (
            "订阅可用但无人开启时 targets_for 必须返回 []"
        )
        count += 1

        empty_detailed = asyncio.run(
            sub_client.targets_detailed(
                700000001,
                "live",
            )
        )
        assert empty_detailed == [], (
            "订阅可用但无人开启时 targets_detailed 必须返回 []"
        )
        count += 1

        sub_client.get_snapshot = original_get_snapshot

        async def no_token_case():
            sub_client.reset_cache()
            sub_client.NOTIFY_API_TOKEN = ""
            fake = FakeSession(
                [FakeResponse(200, _snapshot())]
            )
            sub_client._session = fake
            value = await sub_client.get_snapshot(
                force=True
            )
            return value, fake

        no_token_value, no_token_session = asyncio.run(
            no_token_case()
        )
        assert no_token_value is None, (
            "token 为空必须返回 None"
        )
        count += 1
        assert len(no_token_session.calls) == 0, (
            "token 为空不得发出 HTTP 请求"
        )
        count += 1

        # ★ 会话创建失败也必须返回 None，而不是抛异常。
        async def session_creation_fails():
            sub_client.reset_cache()
            sub_client.NOTIFY_API_TOKEN = "test-token"
            sub_client._session = None

            async def boom():
                raise RuntimeError("会话创建失败")

            original_get_session = sub_client._get_session
            sub_client._get_session = boom
            try:
                return await sub_client.targets_for(
                    700000001,
                    "live",
                )
            finally:
                sub_client._get_session = (
                    original_get_session
                )

        assert (
            asyncio.run(
                session_creation_fails()
            )
            is None
        ), (
            "HTTP 会话创建失败时 targets_for "
            "必须返回 None（不得抛异常）"
        )
        count += 1

    except AssertionError as exc:
        print(f"FAIL sub_client: {exc}")
        return 1
    except Exception as exc:
        print(
            f"FAIL sub_client: 未预期异常 "
            f"{type(exc).__name__}: {exc}"
        )
        return 1
    finally:
        sub_client.NOTIFY_API_TOKEN = original_token
        sub_client._session = original_session
        sub_client.get_snapshot = original_get_snapshot
        sub_client.reset_cache()

    print(f"OK sub_client {count} 条断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
