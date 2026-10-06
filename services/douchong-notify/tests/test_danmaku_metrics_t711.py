"""T-711 regression tests for the danmaku :latest session pointer.

Run directly with Python; no pytest and no real Redis server are required.
"""
from __future__ import annotations

from pathlib import Path
import sys
import types


class FakeRedisError(Exception):
    pass


class FakeClient:
    def __init__(self, *, sadd_result=1, get_values=None, zrange_values=None, raise_on=None):
        self.sadd_result = sadd_result
        self.get_values = dict(get_values or {})
        self.zrange_values = dict(zrange_values or {})
        self.raise_on = raise_on
        self.calls = []

    def _maybe_raise(self, method):
        if self.raise_on == method:
            raise FakeRedisError(f"forced {method} failure")

    def sadd(self, key, value):
        self.calls.append(("sadd", key, value))
        self._maybe_raise("sadd")
        return self.sadd_result

    def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))
        self._maybe_raise("expire")
        return True

    def set(self, key, value, *, ex=None):
        self.calls.append(("set", key, value, ex))
        self._maybe_raise("set")
        return True

    def zincrby(self, key, amount, member):
        self.calls.append(("zincrby", key, amount, member))
        self._maybe_raise("zincrby")
        return 1.0

    def get(self, key):
        self.calls.append(("get", key))
        self._maybe_raise("get")
        return self.get_values.get(key)

    def zrevrange(self, key, start, stop, *, withscores=False):
        self.calls.append(("zrevrange", key, start, stop, withscores))
        self._maybe_raise("zrevrange")
        return self.zrange_values.get(key, [])


def load_module():
    repo_root = Path(__file__).resolve().parents[3]
    source = repo_root / "services" / "douchong-report" / "danmaku_metrics.py"
    package_name = "_t711_danmaku_pkg"
    module_name = f"{package_name}.danmaku_metrics"

    fake_redis = types.ModuleType("redis")
    fake_redis.RedisError = FakeRedisError

    class RedisFactory:
        @classmethod
        def from_url(cls, _url, decode_responses=True):
            return FakeClient()

    fake_redis.Redis = RedisFactory

    package = types.ModuleType(package_name)
    package.__path__ = [str(source.parent)]
    config = types.ModuleType(f"{package_name}.config")
    config.REDIS_URL = "redis://not-used-by-tests/1"

    saved = {
        name: sys.modules.get(name)
        for name in (
            "redis",
            package_name,
            f"{package_name}.config",
            module_name,
        )
    }
    try:
        sys.modules["redis"] = fake_redis
        sys.modules[package_name] = package
        sys.modules[f"{package_name}.config"] = config

        module = types.ModuleType(module_name)
        module.__file__ = str(source)
        module.__package__ = package_name
        sys.modules[module_name] = module

        code = compile(source.read_text(), str(source), "exec")
        exec(code, module.__dict__)
        return module
    finally:
        for name, old_value in saved.items():
            if old_value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_value


def set_client(module, client):
    module.client = client
    return client


def test_new_normal_danmaku_writes_latest_and_words(module):
    client = set_client(module, FakeClient())
    module.record(101, 20261006, "evt-1", "测试弹幕 hello")

    assert (
        "set",
        "qqreport:danmaku:v1:101:latest",
        20261006,
        module.TTL,
    ) in client.calls
    assert sum(call[0] == "set" for call in client.calls) == 1
    assert any(
        call[0] == "zincrby"
        and call[1] == "qqreport:danmaku:v1:101:20261006:words"
        for call in client.calls
    )


def test_duplicate_event_returns_before_latest_and_words(module):
    client = set_client(module, FakeClient(sadd_result=0))
    module.record(101, 20261006, "evt-dup", "重复弹幕")

    assert client.calls == [
        ("sadd", "qqreport:danmaku:v1:101:20261006:dedupe", "evt-dup")
    ]
    assert not any(call[0] == "set" for call in client.calls)
    assert not any(call[0] == "zincrby" for call in client.calls)


def test_emote_writes_latest_and_emotes_but_not_words(module):
    client = set_client(module, FakeClient())
    module.record(
        102,
        77,
        "evt-emote",
        "ignored",
        is_emote=True,
        emote_name="doge",
    )

    assert (
        "set",
        "qqreport:danmaku:v1:102:latest",
        77,
        module.TTL,
    ) in client.calls
    assert (
        "zincrby",
        "qqreport:danmaku:v1:102:77:emotes",
        1,
        "doge",
    ) in client.calls
    assert not any(
        call[0] == "zincrby" and call[1].endswith(":words")
        for call in client.calls
    )


def test_missing_or_zero_session_writes_nothing(module):
    for session in (None, 0):
        client = set_client(module, FakeClient())
        module.record(103, session, "evt-none", "不会写入")

        assert client.calls == []


def test_redis_error_is_swallowed(module):
    client = set_client(module, FakeClient(raise_on="set"))
    survived = False

    module.record(104, 88, "evt-error", "Redis 异常")
    survived = True

    assert survived
    assert any(call[0] == "set" for call in client.calls)
    assert not any(call[0] == "zincrby" for call in client.calls)


def test_read_uses_latest_when_present(module):
    latest_key = "qqreport:danmaku:v1:105:latest"
    prefix = "qqreport:danmaku:v1:105:456"

    client = set_client(
        module,
        FakeClient(
            get_values={
                latest_key: "456",
            },
            zrange_values={
                prefix + ":words": [
                    ("测试", 3.0),
                ],
                prefix + ":emotes": [
                    ("doge", 2.0),
                ],
            },
        ),
    )

    result = module.read(105)

    assert result["available"] is True
    assert result["session_id"] == 456
    assert result["words"] == [
        {
            "word": "测试",
            "count": 3,
        }
    ]
    assert result["emotes"] == [
        {
            "name": "doge",
            "count": 2,
        }
    ]
    assert ("get", latest_key) in client.calls


def test_read_without_latest_reports_unavailable(module):
    client = set_client(module, FakeClient())

    result = module.read(106)

    assert result == {
        "available": False,
        "reason": "尚无短期弹幕统计",
    }
    assert client.calls == [
        ("get", "qqreport:danmaku:v1:106:latest")
    ]


def test_read_explicit_session_skips_latest_lookup(module):
    prefix = "qqreport:danmaku:v1:107:999"

    client = set_client(
        module,
        FakeClient(
            zrange_values={
                prefix + ":words": [
                    ("直接", 1.0),
                ],
                prefix + ":emotes": [],
            },
        ),
    )

    result = module.read(107, 999)

    assert result["available"] is True
    assert result["session_id"] == 999
    assert not any(call[0] == "get" for call in client.calls)
    assert any(
        call[0] == "zrevrange"
        and call[1] == prefix + ":words"
        for call in client.calls
    )


def test_latest_ttl_matches_aggregate_ttl(module):
    client = set_client(module, FakeClient())

    module.record(108, 321, "evt-ttl", "TTL 检查")

    latest_sets = [
        call
        for call in client.calls
        if call[0] == "set"
    ]

    assert module.TTL == 7 * 86400
    assert latest_sets == [
        (
            "set",
            "qqreport:danmaku:v1:108:latest",
            321,
            module.TTL,
        )
    ]
    assert latest_sets[0][3] == module.TTL


def main():
    module = load_module()

    tests = [
        test_new_normal_danmaku_writes_latest_and_words,
        test_duplicate_event_returns_before_latest_and_words,
        test_emote_writes_latest_and_emotes_but_not_words,
        test_missing_or_zero_session_writes_nothing,
        test_redis_error_is_swallowed,
        test_read_uses_latest_when_present,
        test_read_without_latest_reports_unavailable,
        test_read_explicit_session_skips_latest_lookup,
        test_latest_ttl_matches_aggregate_ttl,
    ]

    for test in tests:
        test(module)

    print(f"OK: {len(tests)} T-711 tests passed")


if __name__ == "__main__":
    main()
