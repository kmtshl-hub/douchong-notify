# Standard-library-only assertion script for notify.sender.

import asyncio
import logging
import pathlib
import sys

logging.getLogger("bili_douchong.notify").disabled = True

SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from notify import sender


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

    def post(self, url, *, json, headers, timeout):
        self.calls.append(
            {
                "url": url,
                "json": json,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        if not self.items:
            raise AssertionError("FakeSession 没有剩余响应")
        return FakeRequestContext(self.items.pop(0))

    async def close(self):
        self.closed = True


def _ok():
    return FakeResponse(200, {"retcode": 0, "status": "ok"})


def _failed():
    return FakeResponse(200, {"retcode": 1, "status": "failed"})


def main():
    count = 0
    original_token = sender.ONEBOT_HTTP_TOKEN

    try:
        no_cover = sender.build_text_message("hello")
        assert no_cover == [
            {"type": "text", "data": {"text": "hello"}}
        ], "无封面必须只有 text 段"
        count += 1

        with_cover = sender.build_text_message(
            "hello", "https://example.invalid/cover.jpg"
        )
        assert len(with_cover) == 2, "有封面必须返回两段"
        count += 1
        assert with_cover[1] == {
            "type": "image",
            "data": {"file": "https://example.invalid/cover.jpg"},
        }, "第二段必须是 image"
        count += 1

        fallback_with_cover = sender.build_fallback_text(
            "hello", "https://example.invalid/cover.jpg"
        )
        assert (
            "封面：https://example.invalid/cover.jpg"
            in fallback_with_cover
        ), "有封面兜底必须包含“封面：”"
        count += 1
        assert (
            "封面：" not in sender.build_fallback_text("hello")
        ), "无封面兜底不得包含“封面：”"
        count += 1

        sender.ONEBOT_HTTP_TOKEN = ""
        success_session = FakeSession([_ok()])
        success = asyncio.run(
            sender.send_group(
                900000001,
                sender.build_text_message("hello"),
                "hello",
                session=success_session,
            )
        )
        assert success is True, "一次成功必须返回 True"
        count += 1
        assert len(success_session.calls) == 1, "一次成功只能 POST 1 次"
        count += 1
        assert (
            "Authorization" not in success_session.calls[0]["headers"]
        ), "token 为空不得带 Authorization"
        count += 1

        failure_session = FakeSession([_failed(), _failed()])
        failed = asyncio.run(
            sender.send_group(
                900000001,
                sender.build_text_message(
                    "hello", "https://example.invalid/cover.jpg"
                ),
                "hello\n封面：https://example.invalid/cover.jpg",
                session=failure_session,
            )
        )
        assert (
            len(failure_session.calls) == 2
        ), "首次失败必须再发一次纯文本兜底"
        count += 1
        assert (
            failure_session.calls[1]["json"]["message"]
            == "hello\n封面：https://example.invalid/cover.jpg"
        ), "第二次必须发送 fallback 纯文本"
        count += 1
        assert failed is False, "两次都失败必须返回 False"
        count += 1

        sender.ONEBOT_HTTP_TOKEN = "test-token"
        token_session = FakeSession([_ok()])
        token_result = asyncio.run(
            sender.send_group(
                900000001,
                sender.build_text_message("hello"),
                "hello",
                session=token_session,
            )
        )
        assert token_result is True, "带 token 成功响应必须返回 True"
        count += 1
        assert (
            token_session.calls[0]["headers"].get("Authorization")
            == "Bearer test-token"
        ), "token 非空必须带 Bearer Authorization"
        count += 1

        # ★ 会话创建失败也必须返回 False（调用方按 bool 判断成败，不应抛异常）。
        async def session_creation_fails():
            original_get_session = sender._get_session
            sender._session = None

            async def boom():
                raise RuntimeError("会话创建失败")

            sender._get_session = boom
            try:
                return await sender.send_group(
                    900000001, sender.build_text_message("hello"), "hello"
                )
            finally:
                sender._get_session = original_get_session

        assert (
            asyncio.run(session_creation_fails()) is False
        ), "OneBot 会话创建失败时 send_group 必须返回 False"
        count += 1

    except AssertionError as exc:
        print(f"FAIL sender: {exc}")
        return 1
    except Exception as exc:
        print(f"FAIL sender: 未预期异常 {type(exc).__name__}: {exc}")
        return 1
    finally:
        sender.ONEBOT_HTTP_TOKEN = original_token

    print(f"OK sender {count} 条断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
