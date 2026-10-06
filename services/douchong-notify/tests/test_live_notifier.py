
# Standard-library-only assertion script for notify.live_notifier.

import asyncio
import logging
import pathlib
import sys

SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from notify import live_notifier
from notify import sub_client as real_sub_client


class FakeSubClient:
    def __init__(self, targets, reason=None):
        self.targets = targets
        self.reason = reason
        self.calls = []
        self.closed = False

    async def targets_detailed(self, room_id, event):
        self.calls.append((room_id, event))
        return self.targets

    def _failure_reason(self):
        return self.reason

    async def close(self):
        self.closed = True


class FakeDedup:
    def __init__(self, results=None):
        self.results = list(results or [True])
        self.calls = []
        self.closed = False

    async def first_seen(self, event, room_id, event_id, ttl_seconds):
        self.calls.append((event, room_id, event_id, ttl_seconds))
        if not self.results:
            raise AssertionError("FakeDedup 没有剩余结果")
        return self.results.pop(0)

    async def close(self):
        self.closed = True


class FakeSender:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []
        self.closed = False

    def build_text_message(self, text, cover=""):
        message = [{"type": "text", "data": {"text": text}}]
        if cover:
            message.append(
                {"type": "image", "data": {"file": cover}}
            )
        return message

    def build_fallback_text(self, text, cover=""):
        return f"{text}\n封面：{cover}" if cover else text

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
        if self.results:
            return self.results.pop(0)
        return True

    async def close(self):
        self.closed = True


class WarningCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _data(**overrides):
    value = {
        "name": "主播甲",
        "title": "今晚直播",
        "area": "虚拟 / 生活",
        "cover": "",
        "time_text": "2026-10-05 18:30:00",
        "duration_text": "1小时23分钟",
    }
    value.update(overrides)
    return value


def _install(sub, dedup, sender):
    live_notifier.sub_client = sub
    live_notifier.dedup = dedup
    live_notifier.sender = sender

def _t703_card_orchestration_assertions():
    count = 0
    original_sub = live_notifier.sub_client
    original_dedup = live_notifier.dedup
    original_sender = live_notifier.sender
    original_loader = live_notifier.dynamic_render._load_pillow
    original_live_renderer = live_notifier.dynamic_render.render_live_start
    original_end_renderer = live_notifier.dynamic_render.render_live_end

    class CardSender(FakeSender):
        def __init__(self, *, cover_bytes=None):
            super().__init__()
            self.cover_bytes = cover_bytes
            self.fetch_calls = []

        async def fetch_image_bytes(self, url):
            self.fetch_calls.append(url)
            return self.cover_bytes

        def build_inline_image_message(self, image_bytes):
            if not image_bytes:
                return []
            return [
                {
                    "type": "image",
                    "data": {"file": "base64://test"},
                }
            ]

    try:
        sub = FakeSubClient(
            [{"group_id": 900000001, "at_all": False}]
        )
        card_sender = CardSender(cover_bytes=b"cover")
        _install(sub, FakeDedup([True]), card_sender)
        live_notifier.dynamic_render._load_pillow = lambda: None

        fallback_result = asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(
                    cover="https://example.invalid/c.jpg"
                ),
                session_id=70301,
            )
        )
        expected_text = live_notifier.build_live_text(
            name="主播甲",
            title="今晚直播",
            area="虚拟 / 生活",
            time_text="2026-10-05 18:30:00",
            room_id=700000001,
        )
        assert (
            fallback_result["sent"] is True
            and [
                segment["type"]
                for segment in card_sender.calls[0]["rich"]
            ]
            == ["text"]
            and card_sender.calls[0]["rich"][0]["data"]["text"]
            == expected_text
        ), "Pillow 缺失时必须独立回落到原始纯文本并照常发送"
        count += 1

        live_notifier.dynamic_render._load_pillow = original_loader
        captured = []

        def fake_live_renderer(data, cover_bytes=None):
            captured.append((data, cover_bytes))
            return b"png"

        live_notifier.dynamic_render.render_live_start = fake_live_renderer
        sub = FakeSubClient(
            [{"group_id": 900000001, "at_all": False}]
        )
        failed_cover_sender = CardSender(cover_bytes=None)
        _install(sub, FakeDedup([True]), failed_cover_sender)

        asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(
                    cover="https://example.invalid/missing.jpg"
                ),
                session_id=70302,
            )
        )
        assert (
            failed_cover_sender.fetch_calls
            == ["https://example.invalid/missing.jpg"]
            and captured[0][1] is None
            and [
                segment["type"]
                for segment in failed_cover_sender.calls[0]["rich"]
            ]
            == ["image", "text"]
        ), "封面下载失败必须跳过封面并继续发送开播卡片"
        count += 1

        live_notifier.dynamic_render.render_live_end = lambda data: b"end-png"
        sub = FakeSubClient(
            [{"group_id": 900000001, "at_all": False}]
        )
        end_sender = CardSender()
        _install(sub, FakeDedup([True]), end_sender)
        asyncio.run(
            live_notifier.notify_live_off(
                700000001,
                data=_data(),
                session_id=70303,
            )
        )
        assert [
            segment["type"]
            for segment in end_sender.calls[0]["rich"]
        ] == ["image"], "下播通知必须走无封面内联图片卡片"
        count += 1

    finally:
        live_notifier.sub_client = original_sub
        live_notifier.dedup = original_dedup
        live_notifier.sender = original_sender
        live_notifier.dynamic_render._load_pillow = original_loader
        live_notifier.dynamic_render.render_live_start = original_live_renderer
        live_notifier.dynamic_render.render_live_end = original_end_renderer

    print(f"OK T-703 live_notifier {count} 条断言")


if __name__ == "__main__":
    _t703_card_orchestration_assertions()

def main():
    count = 0
    original_sub = live_notifier.sub_client
    original_dedup = live_notifier.dedup
    original_sender = live_notifier.sender
    original_enabled = live_notifier.NOTIFY_LIVE_ENABLED
    original_initial_silent = live_notifier.INITIAL_SILENT

    capture = WarningCapture()
    live_notifier.LOGGER.addHandler(capture)
    live_notifier.LOGGER.setLevel(logging.WARNING)

    try:
        expected_live = (
            "【开播通知】\n主播甲 开播啦~\n"
            "直播标题：今晚直播\n"
            "直播分区：虚拟 / 生活\n"
            "开播时间：2026-10-05 18:30:00\n"
            "直播间：https://live.bilibili.com/700000001"
        )
        actual_live = live_notifier.build_live_text(
            name="主播甲",
            title="今晚直播",
            area="虚拟 / 生活",
            time_text="2026-10-05 18:30:00",
            room_id=700000001,
        )
        assert actual_live == expected_live, "开播文案必须逐字一致"
        count += 1
        assert actual_live.count("\n") == 5, (
            "开播文案必须恰有 5 个换行"
        )
        count += 1

        expected_off = (
            "【下播通知】\n主播甲 下播了\n"
            "本场时长：1小时23分钟"
        )
        assert (
            live_notifier.build_live_off_text(
                name="主播甲",
                duration_text="1小时23分钟",
            )
            == expected_off
        ), "下播文案必须逐字一致"
        count += 1

        normalized = live_notifier.build_live_text(
            name="  主播\r\n甲  ",
            title="标题\n第二行",
            area="虚拟 / 生活",
            time_text="T",
            room_id=700000001,
        )
        assert "主播 甲 开播啦~" in normalized, (
            "name 换行必须折成单个空格并 strip"
        )
        count += 1
        assert "直播标题：标题 第二行" in normalized, (
            "title 换行必须折成单行"
        )
        count += 1
        assert "直播分区：虚拟 / 生活" in normalized, (
            "普通空格不得被压缩"
        )
        count += 1

        long_name = "名" * 41
        name_line = (
            live_notifier.build_live_text(
                name=long_name,
                title="",
                area="",
                time_text="",
                room_id=700000001,
            )
            .splitlines()[1]
            .removesuffix(" 开播啦~")
        )
        assert (
            len(name_line) == 41
            and name_line.endswith("…")
        ), "name 超长必须 40 码点 + 省略号"
        count += 1

        long_title = "题" * 81
        title_line = (
            live_notifier.build_live_text(
                name="",
                title=long_title,
                area="",
                time_text="",
                room_id=700000001,
            )
            .splitlines()[2]
            .removeprefix("直播标题：")
        )
        assert (
            len(title_line) == 81
            and title_line.endswith("…")
        ), "title 超长必须 80 码点 + 省略号"
        count += 1

        sub = FakeSubClient(
            [{"group_id": 900000001, "at_all": False}]
        )
        fake_dedup = FakeDedup()
        fake_sender = FakeSender()
        _install(sub, fake_dedup, fake_sender)
        live_notifier.NOTIFY_LIVE_ENABLED = False

        result = asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(),
                session_id=98765,
            )
        )
        assert result["skipped"] == "disabled", (
            "总开关关闭必须标记 disabled"
        )
        count += 1
        assert (
            len(sub.calls) == 0
            and len(fake_sender.calls) == 0
        ), "disabled 不得查订阅或发送"
        count += 1

        live_notifier.NOTIFY_LIVE_ENABLED = True
        live_notifier.INITIAL_SILENT = True
        sub = FakeSubClient(
            [{"group_id": 900000001, "at_all": False}]
        )
        fake_sender = FakeSender()
        _install(sub, FakeDedup(), fake_sender)

        result = asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(),
                initial=True,
                session_id=98765,
            )
        )
        assert result["skipped"] == "initial_silent", (
            "首见静默必须先于订阅查询"
        )
        count += 1
        assert (
            len(sub.calls) == 0
            and len(fake_sender.calls) == 0
        ), "首见静默不得查订阅或发送"
        count += 1

        sub = FakeSubClient(None, reason="HTTP 503")
        fake_sender = FakeSender()
        _install(sub, FakeDedup(), fake_sender)

        result = asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(),
                session_id=98765,
            )
        )
        assert (
            result["skipped"] == "unavailable"
            and not fake_sender.calls
        ), "订阅不可用必须拒绝发送"
        count += 1
        assert result["reason"] == "HTTP 503", (
            "订阅不可用必须返回非空原因"
        )
        count += 1

        sub = FakeSubClient([])
        fake_sender = FakeSender()
        _install(sub, FakeDedup(), fake_sender)

        result = asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(),
                session_id=98765,
            )
        )
        assert (
            result["skipped"] == "no_targets"
            and not fake_sender.calls
        ), "空目标必须正常 no-op"
        count += 1
        assert result["reason"] is None, (
            "订阅可用时 reason 必须为 None"
        )
        count += 1

        targets = [
            {"group_id": 900000001, "at_all": True},
            {"group_id": 900000002, "at_all": False},
        ]
        sub = FakeSubClient(targets)
        fake_sender = FakeSender()
        fake_dedup = FakeDedup([True])
        _install(sub, fake_dedup, fake_sender)

        result = asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(),
                session_id=98765,
            )
        )
        assert result == {
            "sent": True,
            "skipped": None,
            "targets": [900000001, 900000002],
            "reason": None,
        }, "正常发送小结必须可断言"
        count += 1

        at_segment = {
            "type": "at",
            "data": {"qq": "all"},
        }
        assert fake_sender.calls[0]["rich"][0] == at_segment, (
            "at_all=True 必须把 at 段放最前"
        )
        count += 1
        assert at_segment not in fake_sender.calls[1]["rich"], (
            "at_all=False 的群绝不能继承其他群的 at"
        )
        count += 1

        sub = FakeSubClient(
            [{"group_id": 900000001, "at_all": False}]
        )
        fake_sender = FakeSender()
        fake_dedup = FakeDedup([True, False])
        _install(sub, fake_dedup, fake_sender)

        first = asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(),
                session_id=98765,
            )
        )
        second = asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(),
                session_id=98765,
            )
        )
        assert (
            first["sent"] is True
            and second["skipped"] == "duplicate"
        ), "第二次去重命中必须跳过"
        count += 1
        assert len(fake_sender.calls) == 1, (
            "duplicate 不得再次发送"
        )
        count += 1

        event, room_id, event_id, ttl = fake_dedup.calls[0]
        assert (
            real_sub_client.dedup_key(
                event,
                room_id,
                event_id,
            )
            == "qqnotify:dedup:live:700000001:98765"
        ), "去重键必须逐字一致"
        count += 1
        assert ttl == 86400, (
            "live/liveEnd 去重 TTL 必须为 24h"
        )
        count += 1

        capture.messages.clear()
        sub = FakeSubClient(
            [{"group_id": 900000001, "at_all": False}]
        )
        fake_dedup = FakeDedup([])
        fake_sender = FakeSender()
        _install(sub, fake_dedup, fake_sender)

        result = asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(),
                session_id=None,
            )
        )
        assert (
            result["sent"] is True
            and result["skipped"] is None
        ), "session_id=None 必须照发"
        count += 1
        assert not fake_dedup.calls, (
            "session_id=None 不得调用去重"
        )
        count += 1
        assert any(
            "缺少 session_id" in message
            for message in capture.messages
        ), "session_id=None 必须 warn"
        count += 1

        sub = FakeSubClient(
            [{"group_id": 900000001, "at_all": False}]
        )
        fake_sender = FakeSender()
        _install(sub, FakeDedup([True]), fake_sender)

        asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(
                    cover="https://example.invalid/c.jpg"
                ),
                session_id=1,
            )
        )
        assert [
            segment["type"]
            for segment in fake_sender.calls[0]["rich"]
        ] == ["text", "image"], (
            "有 cover 必须是 [text, image]"
        )
        count += 1

        sub = FakeSubClient(
            [{"group_id": 900000001, "at_all": False}]
        )
        fake_sender = FakeSender()
        _install(sub, FakeDedup([True]), fake_sender)

        asyncio.run(
            live_notifier.notify_live(
                700000001,
                data=_data(cover=""),
                session_id=1,
            )
        )
        assert [
            segment["type"]
            for segment in fake_sender.calls[0]["rich"]
        ] == ["text"], "无 cover 必须只有 text"
        count += 1

        sub = FakeSubClient(targets)
        fake_sender = FakeSender([False, True])
        _install(sub, FakeDedup([True]), fake_sender)

        result = asyncio.run(
            live_notifier.notify_live_off(
                700000001,
                data=_data(),
                session_id=22,
            )
        )
        assert len(fake_sender.calls) == 2, (
            "某群失败不得阻断后续群发送"
        )
        count += 1
        assert (
            result["sent"] is True
            and sub.calls == [(700000001, "liveEnd")]
        ), "liveEnd 必须独立选目标且任一成功即 sent=True"
        count += 1
        assert any(
            segment["type"] == "text"
            for segment in fake_sender.calls[0]["rich"]
        ), "liveEnd 必须构造文本消息"
        count += 1

        sub = FakeSubClient([])
        fake_dedup = FakeDedup()
        fake_sender = FakeSender()
        _install(sub, fake_dedup, fake_sender)

        asyncio.run(live_notifier.close())
        assert (
            sub.closed
            and fake_dedup.closed
            and fake_sender.closed
        ), "close 必须依次关闭三层连接"
        count += 1

    except AssertionError as exc:
        print(f"FAIL live_notifier: {exc}")
        return 1
    except Exception as exc:
        print(
            f"FAIL live_notifier: 未预期异常 "
            f"{type(exc).__name__}: {exc}"
        )
        return 1
    finally:
        live_notifier.sub_client = original_sub
        live_notifier.dedup = original_dedup
        live_notifier.sender = original_sender
        live_notifier.NOTIFY_LIVE_ENABLED = original_enabled
        live_notifier.INITIAL_SILENT = original_initial_silent
        live_notifier.LOGGER.removeHandler(capture)

    print(f"OK live_notifier {count} 条断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
