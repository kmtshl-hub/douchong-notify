
# Standard-library-only assertion script for notify.guest_entry_notifier.
# 覆盖工单 W-4a-4 的 T-GE-01 ~ T-GE-08（外加 5 条补充）。

import asyncio
import logging
import pathlib
import sys

SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from notify import guest_entry_notifier


class FakeSubClient:
    """stand-in for notify.sub_client —— targets=None 表示「订阅不可用」。"""

    def __init__(self, targets=None, reason=None, raises=False):
        self.targets = targets
        self.reason = reason
        self.raises = raises
        self.calls = []

    async def guest_targets(self, uid):
        self.calls.append(uid)
        if self.raises:
            raise RuntimeError("sub_client boom")
        return self.targets

    def _failure_reason(self):
        return self.reason


class FakeDedup:
    def __init__(self, results=None):
        self.results = list([True] if results is None else results)
        self.calls = []

    async def first_seen(self, event, room_id, event_id, ttl_seconds):
        self.calls.append((event, room_id, event_id, ttl_seconds))
        if not self.results:
            raise AssertionError("FakeDedup 没有剩余结果")
        return self.results.pop(0)


class FakeSender:
    def __init__(self, results=None, raises=False):
        self.results = list(results or [])
        self.raises = raises
        self.calls = []

    def build_text_message(self, text, cover=""):
        message = [{"type": "text", "data": {"text": text}}]
        if cover:
            message.append({"type": "image", "data": {"file": cover}})
        return message

    def build_fallback_text(self, text, cover=""):
        return f"{text}\n封面：{cover}" if cover else text

    async def send_group(self, group_id, rich, fallback, *, session=None):
        self.calls.append(
            {"group_id": group_id, "rich": rich, "fallback": fallback}
        )
        if self.raises:
            raise RuntimeError("sender boom")
        if self.results:
            return self.results.pop(0)
        return True


class WarningCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _install(sub, dedup, sender):
    guest_entry_notifier.sub_client = sub
    guest_entry_notifier.dedup = dedup
    guest_entry_notifier.sender = sender


def _notify(**overrides):
    kwargs = {
        "room_id": 700000001,
        "uid": 800000002,
        "uname": "特关甲",
        "session_id": 900001,
    }
    kwargs.update(overrides)
    return guest_entry_notifier.notify_guest_entry(**kwargs)


def main():
    count = 0
    original_sub = guest_entry_notifier.sub_client
    original_dedup = guest_entry_notifier.dedup
    original_sender = guest_entry_notifier.sender
    original_enabled = guest_entry_notifier.NOTIFY_GUEST_ENTRY_ENABLED

    capture = WarningCapture()
    guest_entry_notifier.LOGGER.addHandler(capture)
    guest_entry_notifier.LOGGER.setLevel(logging.WARNING)

    try:
        # ── T-GE-01 文案三要素 ────────────────────────────────────────
        text = guest_entry_notifier.build_guest_entry_text(
            uname="张三", room_id=700000001
        )
        assert "张三" in text, "文案必须含用户名"
        count += 1
        assert "进入了直播间" in text, "文案必须含动作描述"
        count += 1
        assert "https://live.bilibili.com/700000001" in text, "文案必须含直播间链接"
        count += 1
        assert text.startswith("【特关进房】"), "文案必须有标题头"
        count += 1

        # ── T-GE-01b 用户名缺失一律兜底，且绝不渲染 None ────────────────
        for bad in (None, "", "   ", "\n\t", 0):
            bad_text = guest_entry_notifier.build_guest_entry_text(
                uname=bad, room_id=700000001
            )
            assert "未采集" in bad_text, f"坏用户名 {bad!r} 未兜底"
            assert "None" not in bad_text, f"坏用户名 {bad!r} 渲染出了 None"
            count += 1

        # ── T-GE-01c 换行注入被压平（不得撑破单条消息结构）──────────────
        injected = guest_entry_notifier.build_guest_entry_text(
            uname="a\nb\rc", room_id=700000001
        )
        assert "a b c" in injected, "用户名里的换行必须被压成空格"
        count += 1
        assert len(injected.split("\n")) == 3, "文案行数固定为 3 行"
        count += 1

        # ── T-GE-01d 超长用户名按码点截断 ──────────────────────────────
        long_text = guest_entry_notifier.build_guest_entry_text(
            uname="长" * 100, room_id=700000001
        )
        name_segment = long_text.split("\n")[1]
        assert name_segment.startswith("长" * 40), "必须保留前 40 个码点"
        count += 1
        assert "长" * 41 not in name_segment, "必须截断，不能整段带出"
        count += 1

        # ── T-GE-02 总开关关闭：立即 disabled，且不触碰订阅 ─────────────
        sub = FakeSubClient(targets=[900000001])
        guest_entry_notifier.NOTIFY_GUEST_ENTRY_ENABLED = False
        _install(sub, FakeDedup(), FakeSender())
        result = asyncio.run(_notify())
        assert result["skipped"] == "disabled", "关闭时必须 short-circuit"
        count += 1
        assert sub.calls == [], "disabled 时不得查询订阅"
        count += 1
        guest_entry_notifier.NOTIFY_GUEST_ENTRY_ENABLED = original_enabled

        # ── T-GE-07 uid=0：没有身份，不得查询订阅、不得发送 ──────────────
        sub = FakeSubClient(targets=[900000001])
        _install(sub, FakeDedup(), FakeSender())
        result = asyncio.run(_notify(uid=0))
        assert result["skipped"] == "no_uid", "uid=0 必须被独立防线拦下"
        count += 1
        assert sub.calls == [], "uid=0 时不得查询订阅"
        count += 1

        # ── T-GE-04 订阅不可用：None ≠ []，必须区分 ────────────────────
        sub = FakeSubClient(targets=None, reason="HTTP 503")
        _install(sub, FakeDedup(), FakeSender())
        result = asyncio.run(_notify())
        assert result["skipped"] == "unavailable", "订阅拉不到必须 skip 而非发送"
        count += 1
        assert result["reason"] == "HTTP 503", "必须透出订阅层失败原因"
        count += 1
        assert sub.calls == ["800000002"], "查询参数必须是 str(uid)"
        count += 1

        # ── T-GE-05 无目标群：跳过，且不消耗去重名额 ───────────────────
        dedup = FakeDedup()
        sender = FakeSender()
        _install(FakeSubClient(targets=[]), dedup, sender)
        result = asyncio.run(_notify())
        assert result["skipped"] == "no_targets", "没有群订阅该用户 → no_targets"
        count += 1
        assert sender.calls == [], "无目标群不得发送"
        count += 1
        assert dedup.calls == [], "无目标群不得占用去重键（否则后续真进房会被误判重复）"
        count += 1

        # ── T-GE-06 重复：同 uid 同场次第二次必须被去重拦下 ─────────────
        dedup = FakeDedup(results=[False])
        sender = FakeSender()
        _install(FakeSubClient(targets=[900000001]), dedup, sender)
        result = asyncio.run(_notify())
        assert result["skipped"] == "duplicate", "去重命中必须 skip"
        count += 1
        assert sender.calls == [], "重复不得发送"
        count += 1
        event_name, room_arg, event_id, ttl = dedup.calls[0]
        assert event_name == "guestEntry", "事件名必须是 guestEntry"
        count += 1
        assert room_arg == 700000001, "去重键必须含 room_id"
        count += 1
        assert event_id == "800000002:900001", "复合事件键必须是 uid:session_id"
        count += 1
        assert ttl == 8 * 60 * 60, "TTL 必须是 8 小时"
        count += 1

        # ── T-GE-03 正常发送：多群、无 atAll ──────────────────────────
        dedup = FakeDedup(results=[True])
        sender = FakeSender()
        _install(FakeSubClient(targets=[900000001, 900000002]), dedup, sender)
        result = asyncio.run(_notify())
        assert result["sent"] is True, "全部成功 → sent=True"
        count += 1
        assert result["skipped"] is None, "成功路径 skipped 必须为 None"
        count += 1
        assert result["targets"] == [900000001, 900000002], "必须回传目标群清单"
        count += 1
        assert len(sender.calls) == 2, "每个目标群各发一条"
        count += 1
        assert sender.calls[0]["group_id"] == 900000001, "发送对象必须是目标群"
        count += 1
        rich = sender.calls[0]["rich"]
        assert rich[0]["data"]["text"].startswith("【特关进房】"), "富文本首元素是文案"
        count += 1
        assert not any(el.get("type") == "at" for el in rich), (
            "特关进房不得带 atAll —— atAll 是主播订阅字段，本功能没有群级开关"
        )
        count += 1

        # ── T-GE-08 发送抛异常：不外抛，返回摘要 ──────────────────────
        _install(
            FakeSubClient(targets=[900000001]),
            FakeDedup(results=[True]),
            FakeSender(raises=True),
        )
        result = asyncio.run(_notify())
        assert isinstance(result, dict), "异常路径也必须返回 dict"
        count += 1
        assert result["sent"] is False, "全部失败 → sent=False"
        count += 1
        assert result["skipped"] is None, "异常路径 skipped=None（有 targets）"
        count += 1

        # ── T-GE-08b 部分群失败：先失败后成功仍算 sent=True ─────────────
        sender = FakeSender(results=[False, True])
        _install(
            FakeSubClient(targets=[900000001, 900000002]),
            FakeDedup(results=[True]),
            sender,
        )
        result = asyncio.run(_notify())
        assert result["sent"] is True, "只要有一个群成功就是 sent=True"
        count += 1

        # ── T-GE-08c 订阅层自身抛异常：被兜住，不向外冒 ────────────────
        _install(FakeSubClient(raises=True), FakeDedup(), FakeSender())
        result = asyncio.run(_notify())
        assert result["skipped"] == "exception", "订阅层抛异常必须被兜住"
        count += 1

        # ── T-GE-09 端到端兜底：uname 空串时群里看到的是「未采集」───────
        sender = FakeSender()
        _install(FakeSubClient(targets=[900000001]), FakeDedup(results=[True]), sender)
        asyncio.run(_notify(uname=""))
        sent_text = sender.calls[0]["rich"][0]["data"]["text"]
        assert "未采集 进入了直播间" in sent_text, "空用户名必须渲染成「未采集」"
        count += 1

        # ── T-GE-10 同用户不同场次：去重键必须不同 ─────────────────────
        dedup = FakeDedup(results=[True, True])
        sender = FakeSender()
        _install(FakeSubClient(targets=[900000001]), dedup, sender)
        asyncio.run(_notify(session_id=900001))
        asyncio.run(_notify(session_id=900002))
        assert dedup.calls[0][2] != dedup.calls[1][2], (
            "不同场次必须用不同去重键，否则下场直播不推"
        )
        count += 1
        assert len(sender.calls) == 2, "跨场次各推一次"
        count += 1

        # ── T-GE-11 开关语义：env 未设时默认开启 ──────────────────────
        assert guest_entry_notifier.NOTIFY_GUEST_ENTRY_ENABLED is True, (
            "默认（env 未设）必须是开启"
        )
        count += 1

    finally:
        guest_entry_notifier.sub_client = original_sub
        guest_entry_notifier.dedup = original_dedup
        guest_entry_notifier.sender = original_sender
        guest_entry_notifier.NOTIFY_GUEST_ENTRY_ENABLED = original_enabled
        guest_entry_notifier.LOGGER.removeHandler(capture)

    print(f"OK guest_entry_notifier {count} 条断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
