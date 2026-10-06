# Pure-assertion tests for notify.dynamic_fetcher. No pytest/network/Redis/QQ.

import asyncio
import os
import pathlib
import sys
import types

SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

# Mirror the container package layout: /app/app/notify -> app.notify.
app_pkg = types.ModuleType("app")
app_pkg.__path__ = [str(SERVICE_ROOT)]
sys.modules["app"] = app_pkg

gateway_stub = types.ModuleType("app.bilibili_gateway")
gateway_stub.USER_AGENT = "test-agent"
gateway_stub.init_session = lambda: None

async def _ticket(force=False):
    return "ticket"

async def _room_init(room_id):
    return {"uid": room_id + 1000}

gateway_stub.ensure_bili_ticket = _ticket
gateway_stub.fetch_room_init = _room_init

runtime_stub = types.ModuleType("app.runtime_state")
runtime_stub.aiohttp_session = None

sys.modules["app.bilibili_gateway"] = gateway_stub
sys.modules["app.runtime_state"] = runtime_stub

from app.notify import dynamic_fetcher as fetcher


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.set_calls = []

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.set_calls.append((key, value, kwargs))
        self.values[key] = value
        return True


class FakeResponse:
    def __init__(self, payload=None, data=b"img", status=200, headers=None):
        self.payload = payload
        self.data = data
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self.payload

    async def read(self):
        return self.data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _old_item():
    return {
        "id_str": "1001",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {
                "name": "旧",
                "pub_ts": 1_700_000_000,
                "avatar": "BAD!",
                "face": "https://face/good.jpg",
            },
            "module_dynamic": {
                "desc": {
                    "text": "旧正文",
                    "rich_text_nodes": [
                        {
                            "type": "RICH_TEXT_NODE_TYPE_TEXT",
                            "text": "旧正文",
                        }
                    ],
                },
                "major": {
                    "draw": {
                        "items": [{"src": "https://img/old.jpg"}]
                    }
                },
            },
        },
    }


def _new_item():
    return {
        "id_str": "1002",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {
                "name": "新",
                "pub_ts": 1_700_000_001,
                "face": "https://face/new.jpg",
            },
            "module_dynamic": {
                "major": {
                    "opus": {
                        "title": "题",
                        "pics": [{"url": "https://img/new.jpg"}],
                        "summary": {
                            "text": "新正文[长表情]！",
                            "rich_text_nodes": [
                                {
                                    "type": "RICH_TEXT_NODE_TYPE_TEXT",
                                    "text": "新正文",
                                },
                                {
                                    "type": "RICH_TEXT_NODE_TYPE_EMOJI",
                                    "text": "[长表情]",
                                    "emoji": {
                                        "icon_url": "https://emoji/e.png",
                                        "text": "捂脸",
                                    },
                                },
                                {
                                    "type": "RICH_TEXT_NODE_TYPE_TEXT",
                                    "text": "！",
                                },
                            ],
                        },
                    }
                }
            },
        },
    }


async def main_async():
    count = 0

    assert (
        fetcher.gateway is sys.modules["app.bilibili_gateway"]
        and fetcher.runtime_state is sys.modules["app.runtime_state"]
        and "app.notify.bilibili_gateway" not in sys.modules
        and "app.notify.runtime_state" not in sys.modules
    )
    count += 1

    old = fetcher._parse_item(_old_item(), 42)
    assert old["text"] == "旧正文"
    count += 1
    assert old["_image_urls"] == ["https://img/old.jpg"]
    count += 1
    assert old["_avatar_url"] == "https://face/good.jpg"
    assert old["_avatar_url"] != "BAD!"
    count += 1

    new = fetcher._parse_item(_new_item(), 42)
    assert new["text"] == "新正文[长表情]！"
    count += 1
    assert new["_image_urls"] == ["https://img/new.jpg"]
    count += 1
    assert [n["type"] for n in new["rich_nodes"]] == [
        "text", "emoji", "text"
    ]
    count += 1
    assert new["rich_nodes"][1]["url"] == "https://emoji/e.png"
    count += 1

    forwarded = _old_item()
    forwarded["orig"] = _new_item()
    parsed_fwd = fetcher._parse_item(forwarded, 42)["forward"]
    assert parsed_fwd["text"] == "新正文[长表情]！"
    assert parsed_fwd["_image_urls"] == ["https://img/new.jpg"]
    count += 1

    empty = fetcher._parse_item(
        {
            "modules": {
                "module_author": None,
                "module_dynamic": None,
            }
        },
        42,
    )
    assert empty["text"] == ""
    assert empty["images"] == []
    assert empty["timestamp"] is None
    count += 1

    original_session = fetcher.runtime_state.aiohttp_session
    original_enabled = fetcher._enabled
    original_init = fetcher.gateway.init_session
    original_ticket = fetcher.gateway.ensure_bili_ticket

    try:
        fetcher._reset_backoff()
        fetcher._enabled = lambda name, default="1": True
        fetcher.gateway.init_session = lambda: None

        async def ticket(force=False):
            return "x"

        fetcher.gateway.ensure_bili_ticket = ticket

        session = FakeSession(
            [
                FakeResponse({"code": 123, "data": {}}),
                FakeResponse(
                    {
                        "code": 0,
                        "data": {"items": [_old_item()]},
                    }
                ),
            ]
        )
        fetcher.runtime_state.aiohttp_session = session

        assert await fetcher.fetch_once(1) is None
        count += 1

        ok = await fetcher.fetch_once(2)
        assert ok and ok[0]["dynamic_id"] == "1001"
        count += 1

        fetcher._reset_backoff()
        fetcher.runtime_state.aiohttp_session = FakeSession(
            [FakeResponse({"code": -412})]
        )
        assert await fetcher.fetch_once(3) is None
        first_until = fetcher._backoff_until
        assert (
            fetcher._backoff_seconds
            == fetcher.BACKOFF_INITIAL_SECONDS * 2
        )
        count += 1

        calls = len(fetcher.runtime_state.aiohttp_session.calls)
        assert await fetcher.fetch_once(3) is None
        assert len(fetcher.runtime_state.aiohttp_session.calls) == calls
        count += 1

        fetcher._backoff_until = 0
        fetcher.runtime_state.aiohttp_session = FakeSession(
            [FakeResponse({"code": -412})]
        )
        await fetcher.fetch_once(3)
        assert (
            fetcher._backoff_seconds
            == fetcher.BACKOFF_INITIAL_SECONDS * 4
        )
        assert fetcher._backoff_until > first_until
        count += 1

        fetcher._backoff_until = 0
        fetcher.runtime_state.aiohttp_session = FakeSession(
            [
                FakeResponse(
                    {
                        "code": 0,
                        "data": {"items": []},
                    }
                )
            ]
        )
        await fetcher.fetch_once(3)
        assert (
            fetcher._backoff_seconds
            == fetcher.BACKOFF_INITIAL_SECONDS
        )
        assert fetcher._backoff_until == 0
        count += 1

        os.environ["DYNAMIC_POLL_MIN_SECONDS"] = "60"
        os.environ["DYNAMIC_POLL_MAX_SECONDS"] = "90"
        old_uniform = fetcher.random.uniform
        fetcher.random.uniform = lambda a, b: (a + b) / 2
        assert fetcher._poll_delay() == 75
        assert 60 <= fetcher._poll_delay() <= 90
        count += 1
        fetcher.random.uniform = old_uniform

        fetcher._enabled = (
            lambda name, default="1":
            False if name == "NOTIFY_DYNAMIC_ENABLED" else True
        )
        fetcher.runtime_state.aiohttp_session = FakeSession([])
        assert await fetcher.fetch_once(9) is None
        assert fetcher.runtime_state.aiohttp_session.calls == []
        count += 1

    finally:
        fetcher.runtime_state.aiohttp_session = original_session
        fetcher._enabled = original_enabled
        fetcher.gateway.init_session = original_init
        fetcher.gateway.ensure_bili_ticket = original_ticket
        fetcher._reset_backoff()

    redis = FakeRedis()
    seed_calls = []
    old_get_redis = fetcher.dedup._get_redis
    old_first_seen = fetcher.dedup.first_seen

    async def get_redis():
        return redis

    async def first_seen(*args):
        seed_calls.append(args)
        return True

    fetcher.dedup._get_redis = get_redis
    fetcher.dedup.first_seen = first_seen

    try:
        assert await fetcher._read_cursor(42) == (True, None)
        count += 1

        assert await fetcher._seed_latest(
            700000001, 42, [new]
        ) is True

        assert seed_calls == [
            (
                "dynamic",
                700000001,
                "1002",
                fetcher.dynamic_notifier.DEDUP_TTL_SECONDS,
            )
        ]
        count += 1

        assert redis.values[fetcher._initial_key(42)] == "1002"
        count += 1

        assert await fetcher._read_cursor(42) == (True, "1002")
        count += 1

        prefix = fetcher._new_prefix(
            [
                {**new, "dynamic_id": "1004"},
                {**new, "dynamic_id": "1003"},
                new,
            ],
            "1002",
        )
        assert [x["dynamic_id"] for x in prefix] == [
            "1004", "1003"
        ]
        count += 1

    finally:
        fetcher.dedup._get_redis = old_get_redis
        fetcher.dedup.first_seen = old_first_seen

    fetcher.runtime_state.aiohttp_session = FakeSession(
        [
            FakeResponse(
                data=b"x",
                headers={
                    "Content-Length":
                    str(fetcher.MAX_IMAGE_BYTES + 1)
                },
            ),
            RuntimeError("boom"),
            FakeResponse(data=b"ok"),
        ]
    )

    sem = asyncio.Semaphore(fetcher.DOWNLOAD_CONCURRENCY)
    assert await fetcher._download_one(
        "https://big", sem
    ) is None
    count += 1

    assert await fetcher._download_one(
        "https://bad", sem
    ) is None
    assert await fetcher._download_one(
        "https://good", sem
    ) == b"ok"
    count += 1

    async def fake_download(url, semaphore):
        return ("B:" + str(url)).encode() if url else None

    old_download = fetcher._download_one
    fetcher._download_one = fake_download
    try:
        material = await fetcher._materialize(
            fetcher._parse_item(_new_item(), 42)
        )
        assert material["text"] == "新正文[长表情]！"
        count += 1

        assert [
            n["type"] for n in material["rich_nodes"]
        ] == ["text", "emoji", "text"]
        assert (
            material["rich_nodes"][1]["image"]
            == b"B:https://emoji/e.png"
        )
        count += 1

        parsed_forward = fetcher._parse_item(forwarded, 42)["forward"]
        assert (
            [node["type"] for node in parsed_forward["rich_nodes"]]
            == ["text", "emoji", "text"]
            and parsed_forward["rich_nodes"][1]["url"]
            == "https://emoji/e.png"
        )
        count += 1

        forward_material = await fetcher._materialize(
            fetcher._parse_item(forwarded, 42)
        )
        forward_emoji = forward_material["forward"]["rich_nodes"][1]
        assert (
            forward_emoji["image"] == b"B:https://emoji/e.png"
            and "url" not in forward_emoji
        )
        count += 1

    finally:
        fetcher._download_one = old_download

    active = 0
    peak = 0

    async def counted(url, semaphore):
        nonlocal active, peak
        async with semaphore:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return b"x"

    fetcher._download_one = counted
    try:
        many = fetcher._parse_item(_new_item(), 42)
        many["_image_urls"] = [
            f"u{i}" for i in range(9)
        ]
        await fetcher._materialize(many)
        assert peak <= fetcher.DOWNLOAD_CONCURRENCY
        count += 1
    finally:
        fetcher._download_one = old_download

    fetcher._enabled = lambda name, default="1": True
    fetcher.runtime_state.aiohttp_session = FakeSession(
        [RuntimeError("network")]
    )
    assert await fetcher.fetch_once(77) is None
    count += 1
    fetcher._enabled = original_enabled

    print(f"OK dynamic_fetcher {count} 条断言")
    return 0


def main():
    try:
        return asyncio.run(main_async())
    except AssertionError as exc:
        print(f"FAIL dynamic_fetcher: {exc}")
        return 1
    except Exception as exc:
        print(
            "FAIL dynamic_fetcher: "
            f"未预期异常 {type(exc).__name__}: {exc}"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
