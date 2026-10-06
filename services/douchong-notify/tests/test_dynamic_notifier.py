# Pure-assertion tests for notify.dynamic_notifier. No pytest/network/Redis/QQ.

import asyncio
import base64
import pathlib
import sys

SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from notify import dynamic_notifier
from notify import sender as real_sender


class FakeSubClient:
    def __init__(
        self,
        targets=None,
        reason=None,
        raises=False,
    ):
        self.targets = targets
        self.reason = reason
        self.raises = raises
        self.calls = []

    async def targets_detailed(
        self,
        room_id,
        event,
    ):
        self.calls.append(
            (room_id, event)
        )
        if self.raises:
            raise RuntimeError("sub boom")
        return self.targets

    def _failure_reason(self):
        return self.reason


class FakeDedup:
    def __init__(
        self,
        result=True,
        raises=False,
    ):
        self.result = result
        self.raises = raises
        self.calls = []

    async def first_seen(
        self,
        event,
        room_id,
        event_id,
        ttl_seconds,
    ):
        self.calls.append(
            (
                event,
                room_id,
                event_id,
                ttl_seconds,
            )
        )
        if self.raises:
            raise RuntimeError("dedup boom")
        return self.result


class FakeRenderer:
    def __init__(
        self,
        result=b"\x89PNG\r\n\x1a\ncard",
        raises=False,
    ):
        self.result = result
        self.raises = raises
        self.calls = []

    def render_dynamic(self, data):
        self.calls.append(data)
        if self.raises:
            raise RuntimeError("render boom")
        return self.result


class FakeSender:
    def __init__(
        self,
        results=None,
        raises_at=None,
    ):
        self.results = list(
            results or []
        )
        self.raises_at = set(
            raises_at or []
        )
        self.calls = []
        self.inline_calls = []

    def build_inline_image_message(
        self,
        image_bytes,
    ):
        self.inline_calls.append(
            image_bytes
        )
        if not image_bytes:
            return []

        encoded = base64.b64encode(
            image_bytes
        ).decode("ascii")

        return [
            {
                "type": "image",
                "data": {
                    "file": "base64://" + encoded
                },
            }
        ]

    def build_text_message(
        self,
        text,
        cover="",
    ):
        return [
            {
                "type": "text",
                "data": {"text": text},
            }
        ]

    async def send_group(
        self,
        group_id,
        rich,
        fallback,
        *,
        session=None,
    ):
        self.calls.append(
            {
                "group_id": group_id,
                "rich": rich,
                "fallback": fallback,
            }
        )

        if group_id in self.raises_at:
            raise RuntimeError("send boom")

        if self.results:
            return self.results.pop(0)

        return True


def _install(
    sub,
    dedup,
    renderer,
    sender,
):
    dynamic_notifier.sub_client = sub
    dynamic_notifier.dedup = dedup
    dynamic_notifier.dynamic_render = renderer
    dynamic_notifier.sender = sender


def _data(**overrides):
    value = {
        "dynamic_id": "900000001",
        "uid": 800000002,
        "uname": "某主播",
        "timestamp": 1_759_680_000,
        "text": "正文内容",
        "images": [],
        "video": None,
        "forward": None,
    }
    value.update(overrides)
    return value


def _notify(data=None):
    return dynamic_notifier.notify_dynamic(
        700000001,
        data=(
            _data()
            if data is None
            else data
        ),
    )


def main():
    count = 0

    original_sub = dynamic_notifier.sub_client
    original_dedup = dynamic_notifier.dedup
    original_renderer = dynamic_notifier.dynamic_render
    original_sender = dynamic_notifier.sender
    original_enabled = (
        dynamic_notifier.NOTIFY_DYNAMIC_ENABLED
    )

    try:
        sample = b"\x89PNG\r\n\x1a\nabc"
        segment = (
            real_sender.build_inline_image_message(
                sample
            )
        )
        file_value = (
            segment[0]["data"]["file"]
        )

        assert file_value.startswith(
            "base64://"
        ), "内联图片必须使用 base64://"
        count += 1

        encoded = file_value.removeprefix(
            "base64://"
        )
        assert not any(
            ch.isspace()
            for ch in encoded
        ), "base64 串不得含空白"
        count += 1

        assert (
            base64.b64decode(encoded)
            == sample
        ), "base64 必须可无损还原原图 bytes"
        count += 1

        assert (
            real_sender.build_inline_image_message(
                None
            )
            == []
        ), "非法图片输入必须可回落"
        count += 1

        sub = FakeSubClient(
            targets=[
                {
                    "group_id": 900000001,
                    "at_all": False,
                }
            ]
        )
        dedup = FakeDedup(result=True)
        renderer = FakeRenderer()
        send = FakeSender()

        _install(
            sub,
            dedup,
            renderer,
            send,
        )

        result = asyncio.run(_notify())
        assert (
            result["sent"] is True
            and result["skipped"] is None
        ), "正常路径必须发送成功"
        count += 1

        assert sub.calls == [
            (
                700000001,
                "dynamic",
            )
        ], "订阅事件名必须严格为 dynamic"
        count += 1

        assert dedup.calls == [
            (
                "dynamic",
                700000001,
                "900000001",
                48 * 60 * 60,
            )
        ], (
            "去重事件/键/TTL 必须为 "
            "dynamic / dynamic_id / 48h"
        )
        count += 1

        assert len(renderer.calls) == 1, (
            "正常路径必须只渲染一次"
        )
        count += 1

        assert any(
            part.get("type") == "image"
            for part in send.calls[0]["rich"]
        ), "正常路径必须发送卡片 image 段"
        count += 1

        renderer = FakeRenderer()
        send = FakeSender()

        _install(
            FakeSubClient(
                targets=[
                    {
                        "group_id": 900000001,
                        "at_all": False,
                    }
                ]
            ),
            FakeDedup(result=False),
            renderer,
            send,
        )

        result = asyncio.run(_notify())
        assert result["skipped"] == "duplicate", (
            "重复动态必须跳过"
        )
        count += 1

        assert (
            renderer.calls == []
            and send.calls == []
        ), "重复动态不得白渲染/发送"
        count += 1

        sub = FakeSubClient(
            targets=[
                {
                    "group_id": 900000001,
                    "at_all": False,
                }
            ]
        )
        disabled_dedup = FakeDedup()
        disabled_renderer = FakeRenderer()

        dynamic_notifier.NOTIFY_DYNAMIC_ENABLED = False

        _install(
            sub,
            disabled_dedup,
            disabled_renderer,
            FakeSender(),
        )

        result = asyncio.run(_notify())

        assert result["skipped"] == "disabled", (
            "总开关关闭必须 disabled"
        )
        count += 1

        assert sub.calls == [], (
            "disabled 时不得查订阅"
        )
        count += 1

        assert (
            disabled_dedup.calls == []
            and disabled_renderer.calls == []
        ), "disabled 时不得去重或渲染"
        count += 1

        dynamic_notifier.NOTIFY_DYNAMIC_ENABLED = (
            original_enabled
        )

        _install(
            FakeSubClient(
                targets=None,
                reason="HTTP 503",
            ),
            FakeDedup(),
            FakeRenderer(),
            FakeSender(),
        )

        result = asyncio.run(_notify())
        assert (
            result["skipped"] == "unavailable"
            and result["reason"] == "HTTP 503"
        ), "None 必须表示订阅不可用"
        count += 1

        dedup = FakeDedup()
        no_target_renderer = FakeRenderer()

        _install(
            FakeSubClient(targets=[]),
            dedup,
            no_target_renderer,
            FakeSender(),
        )

        result = asyncio.run(_notify())
        assert result["skipped"] == "no_targets", (
            "空列表必须表示无人订阅"
        )
        count += 1

        assert dedup.calls == [], (
            "无目标群不得消耗去重键"
        )
        count += 1

        assert no_target_renderer.calls == [], (
            "无目标群不得白渲染卡片"
        )
        count += 1

        send = FakeSender()

        _install(
            FakeSubClient(
                targets=[
                    {
                        "group_id": 900000001,
                        "at_all": False,
                    }
                ]
            ),
            FakeDedup(),
            FakeRenderer(result=None),
            send,
        )

        result = asyncio.run(_notify())
        assert result["sent"] is True, (
            "渲染失败必须仍能发送"
        )
        count += 1

        assert (
            send.calls[0]["rich"][0]["type"]
            == "text"
        ), "渲染失败必须回落纯文本"
        count += 1

        assert (
            "https://t.bilibili.com/900000001"
            in send.calls[0]["fallback"]
        ), "回落文案必须含原动态链接"
        count += 1

        send = FakeSender()

        _install(
            FakeSubClient(
                targets=[
                    {
                        "group_id": 900000001,
                        "at_all": False,
                    }
                ]
            ),
            FakeDedup(),
            FakeRenderer(raises=True),
            send,
        )

        result = asyncio.run(_notify())
        assert (
            result["sent"] is True
            and send.calls[0]["rich"][0]["type"]
            == "text"
        ), "渲染器异常不得让通知消失"
        count += 1

        sub = FakeSubClient(
            targets=[
                {
                    "group_id": 900000001,
                    "at_all": False,
                }
            ]
        )

        _install(
            sub,
            FakeDedup(),
            FakeRenderer(),
            FakeSender(),
        )

        result = asyncio.run(
            _notify(
                _data(
                    dynamic_id=""
                )
            )
        )

        assert result["skipped"] == "no_dynamic_id", (
            "缺 dynamic_id 必须拒绝"
        )
        count += 1

        assert sub.calls == [], (
            "缺 dynamic_id 不应查询订阅"
        )
        count += 1

        result = asyncio.run(
            dynamic_notifier.notify_dynamic(
                700000001,
                data=None,
            )
        )
        assert result["skipped"] == "invalid_data", (
            "非 dict data 必须返回摘要而不是抛异常"
        )
        count += 1

        send = FakeSender()
        targets = [
            {
                "group_id": 900000001,
                "at_all": True,
            },
            {
                "group_id": 900000002,
                "at_all": False,
            },
        ]

        _install(
            FakeSubClient(targets=targets),
            FakeDedup(),
            FakeRenderer(),
            send,
        )

        asyncio.run(_notify())

        first_rich = send.calls[0]["rich"]
        second_rich = send.calls[1]["rich"]

        assert first_rich[0] == {
            "type": "at",
            "data": {"qq": "all"},
        }, "开 atAll 的群必须首段 @all"
        count += 1

        assert not any(
            part.get("type") == "at"
            for part in second_rich
        ), "未开 atAll 的群不得泄漏 at 段"
        count += 1

        send = FakeSender(
            results=[
                False,
                True,
            ]
        )

        _install(
            FakeSubClient(targets=targets),
            FakeDedup(),
            FakeRenderer(),
            send,
        )

        result = asyncio.run(_notify())
        assert (
            len(send.calls) == 2
            and result["sent"] is True
        ), "单群失败后必须继续后续群"
        count += 1

        send = FakeSender(
            raises_at={900000001}
        )

        _install(
            FakeSubClient(targets=targets),
            FakeDedup(),
            FakeRenderer(),
            send,
        )

        result = asyncio.run(_notify())
        assert (
            len(send.calls) == 2
            and result["sent"] is True
        ), "单群发送抛异常也不得中断后续群"
        count += 1

        send = FakeSender(
            results=[
                False,
                False,
            ]
        )

        _install(
            FakeSubClient(targets=targets),
            FakeDedup(),
            FakeRenderer(),
            send,
        )

        result = asyncio.run(_notify())
        assert result["sent"] is False, (
            "全部群失败才 sent=False"
        )
        count += 1

        send = FakeSender()

        _install(
            FakeSubClient(
                targets=[
                    {
                        "group_id": 900000001,
                        "at_all": False,
                    }
                ]
            ),
            FakeDedup(),
            FakeRenderer(result=None),
            send,
        )

        asyncio.run(
            _notify(
                _data(uname="")
            )
        )

        assert (
            "未知用户 发布了新动态"
            in send.calls[0]["fallback"]
        ), "空昵称必须兜底为未知用户"
        count += 1

        _install(
            FakeSubClient(raises=True),
            FakeDedup(),
            FakeRenderer(),
            FakeSender(),
        )

        result = asyncio.run(_notify())
        assert result["skipped"] == "exception", (
            "订阅层异常必须转成摘要 dict"
        )
        count += 1

        send = FakeSender()

        _install(
            FakeSubClient(
                targets=[
                    {
                        "group_id": 900000001,
                        "at_all": False,
                    }
                ]
            ),
            FakeDedup(raises=True),
            FakeRenderer(),
            send,
        )

        result = asyncio.run(_notify())
        assert (
            result["sent"] is True
            and len(send.calls) == 1
        ), "去重异常应保持 fail-open 发送"
        count += 1

    except AssertionError as exc:
        print(
            f"FAIL dynamic_notifier: {exc}"
        )
        return 1

    except Exception as exc:
        print(
            "FAIL dynamic_notifier: "
            f"未预期异常 {type(exc).__name__}: {exc}"
        )
        return 1

    finally:
        dynamic_notifier.sub_client = original_sub
        dynamic_notifier.dedup = original_dedup
        dynamic_notifier.dynamic_render = original_renderer
        dynamic_notifier.sender = original_sender
        dynamic_notifier.NOTIFY_DYNAMIC_ENABLED = (
            original_enabled
        )

    print(
        f"OK dynamic_notifier {count} 条断言"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
