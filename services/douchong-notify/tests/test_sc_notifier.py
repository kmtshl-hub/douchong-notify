
# Standard-library-only assertion script for notify.sc_notifier.
# 覆盖工单 W-4a-4 的 T-SC-01 ~ T-SC-08（外加 4 条补充）。

import asyncio
import logging
import pathlib
import sys

SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from notify import sc_notifier


class FakeSubClient:
    def __init__(self, detailed=None, reason=None, raises=False):
        self.detailed = detailed
        self.reason = reason
        self.raises = raises
        self.calls = []

    async def targets_detailed(self, room_id, event):
        self.calls.append((room_id, event))
        if self.raises:
            raise RuntimeError("sub_client boom")
        return self.detailed

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


def _detailed(*group_ids, at_all=False):
    return [{"group_id": gid, "at_all": at_all} for gid in group_ids]


def _install(sub, dedup, sender):
    sc_notifier.sub_client = sub
    sc_notifier.dedup = dedup
    sc_notifier.sender = sender


def _notify(**overrides):
    kwargs = {
        "room_id": 700000001,
        "sc_id": "sc:700000001:1",
        "uname": "老板甲",
        "uid": 800000001,
        "price": 50,
        "message": "加油",
        "session_id": 900001,
    }
    kwargs.update(overrides)
    return sc_notifier.notify_sc(**kwargs)


def main():
    count = 0
    original_sub = sc_notifier.sub_client
    original_dedup = sc_notifier.dedup
    original_sender = sc_notifier.sender
    original_enabled = sc_notifier.NOTIFY_SC_ENABLED
    original_min_price = sc_notifier.SC_MIN_PRICE

    capture = WarningCapture()
    sc_notifier.LOGGER.addHandler(capture)
    sc_notifier.LOGGER.setLevel(logging.WARNING)

    try:
        # ── T-SC-01 文案 ──────────────────────────────────────────────
        text = sc_notifier.build_sc_text(
            uname="老板甲", price=50, message="加油", room_id=700000001
        )
        assert text == (
            "【SC 醒目留言】\n老板甲 发送了 ¥50 醒目留言\n"
            "内容：加油\n直播间：https://live.bilibili.com/700000001"
        ), "SC 文案必须逐字一致"
        count += 1
        assert text.count("\n") == 3, "SC 文案必须恰有 3 个换行"
        count += 1

        bare = sc_notifier.build_sc_text(
            uname="", price=None, message="", room_id=700000001
        )
        assert "None" not in bare, "缺字段不得渲染出 None"
        count += 1
        assert "内容：" not in bare, "空留言不得产生「内容：」行"
        count += 1

        # ── T-SC-02 低于阈值 ─────────────────────────────────────────
        _install(FakeSubClient(_detailed(900000001)), FakeDedup(), FakeSender())
        summary = asyncio.run(_notify(price=29))
        assert summary["skipped"] == "below_threshold", "¥29 必须被阈值拦下"
        count += 1
        assert summary["targets"] == [], "低于阈值不得解析出目标群"
        count += 1

        sc_notifier.SC_MIN_PRICE = 100
        summary = asyncio.run(_notify(price=50))
        assert summary["skipped"] == "below_threshold", "阈值改成 100 后 ¥50 也被拦"
        count += 1
        sc_notifier.SC_MIN_PRICE = original_min_price

        # ── T-SC-03 订阅不可用 ───────────────────────────────────────
        sub = FakeSubClient(None, reason="HTTP 500")
        dedup = FakeDedup()
        _install(sub, dedup, FakeSender())
        summary = asyncio.run(_notify())
        assert summary["skipped"] == "unavailable", "订阅读不到必须拒绝发送"
        count += 1
        assert summary["reason"] == "HTTP 500", "必须带上订阅层的失败原因"
        count += 1
        assert dedup.calls == [], "订阅不可用时不得写入去重键"
        count += 1

        # ── T-SC-04 重复 ────────────────────────────────────────────
        sub = FakeSubClient(_detailed(900000001))
        dedup = FakeDedup([True, False])
        sender = FakeSender()
        _install(sub, dedup, sender)
        first = asyncio.run(_notify())
        second = asyncio.run(_notify())
        assert first["sent"] is True, "首次必须发送"
        count += 1
        assert second["skipped"] == "duplicate", "同 sc_id 第二次必须判重"
        count += 1
        assert len(sender.calls) == 1, "判重后不得重复发送"
        count += 1
        assert sub.calls and all(call == (700000001, "sc") for call in sub.calls), (
            "订阅查询必须问 sc 事件开关，写错成 live 会让开播群收到 SC"
        )
        count += 1
        assert dedup.calls[0][0] == "sc", "去重事件名必须是 sc"
        count += 1
        assert dedup.calls[0][2] == "sc:700000001:1", "去重键必须用上游 event_key"
        count += 1
        assert dedup.calls[0][3] == 24 * 60 * 60, "SC 去重 TTL 必须是 24h"
        count += 1

        # ── T-SC-05 atAll ──────────────────────────────────────────
        sub = FakeSubClient(_detailed(900000001, at_all=True))
        sender = FakeSender()
        _install(sub, FakeDedup(), sender)
        asyncio.run(_notify(sc_id="sc:at-all"))
        rich = sender.calls[0]["rich"]
        assert rich[0] == {"type": "at", "data": {"qq": "all"}}, (
            "atAll 群的消息首个元素必须是 at-all"
        )
        count += 1

        sub = FakeSubClient(_detailed(900000001, at_all=False))
        sender = FakeSender()
        _install(sub, FakeDedup(), sender)
        asyncio.run(_notify(sc_id="sc:no-at"))
        rich = sender.calls[0]["rich"]
        assert rich[0]["type"] == "text", "未开 atAll 的群不得出现 at 元素"
        count += 1

        # ── T-SC-06 用户名为空 ───────────────────────────────────────
        _install(
            FakeSubClient(_detailed(900000001)),
            FakeDedup([True, True, True]),
            FakeSender(),
        )
        for empty in ("", None, "   "):
            summary = asyncio.run(_notify(uname=empty, sc_id=f"sc:empty:{empty!r}"))
            assert summary["sent"] is True, "用户名缺失仍应发送"
            count += 1
        assert "未采集" in sc_notifier.build_sc_text(
            uname="", price=30, message="x", room_id=700000001
        ), "用户名缺失必须显示「未采集」"
        count += 1

        # ── T-SC-07 发送抛异常不外溢 ─────────────────────────────────
        _install(FakeSubClient(_detailed(900000001)), FakeDedup(), FakeSender(raises=True))
        summary = asyncio.run(_notify(sc_id="sc:raise"))
        assert isinstance(summary, dict) and summary["sent"] is False, (
            "send_group 抛异常时 notify_sc 必须返回摘要而不是抛出"
        )
        count += 1
        assert any("SC 群通知发送异常" in m for m in capture.messages), (
            "发送异常必须留下 warning 日志"
        )
        count += 1

        # ── T-SC-08 总开关关闭 ───────────────────────────────────────
        sub = FakeSubClient(_detailed(900000001))
        _install(sub, FakeDedup(), FakeSender())
        sc_notifier.NOTIFY_SC_ENABLED = False
        summary = asyncio.run(_notify())
        assert summary["skipped"] == "disabled", "总开关关闭必须立即返回 disabled"
        count += 1
        assert sub.calls == [], "关闭时不得触碰订阅层"
        count += 1
        sc_notifier.NOTIFY_SC_ENABLED = original_enabled

        # ── 补充 1：无目标群 ─────────────────────────────────────────
        _install(FakeSubClient([]), FakeDedup(), FakeSender())
        summary = asyncio.run(_notify())
        assert summary["skipped"] == "no_targets", "没有群订阅 sc 时必须 no_targets"
        count += 1

        # ── 补充 2：session_id 缺失仍发送，但不去重 ───────────────────
        dedup = FakeDedup()
        sender = FakeSender()
        _install(FakeSubClient(_detailed(900000001)), dedup, sender)
        summary = asyncio.run(_notify(session_id=None))
        assert summary["sent"] is True, "缺 session_id 时仍要发送"
        count += 1
        assert dedup.calls == [], "缺 session_id 时跳过去重"
        count += 1

        # ── 补充 3：订阅层抛异常 → 摘要 exception ─────────────────────
        _install(FakeSubClient(raises=True), FakeDedup(), FakeSender())
        summary = asyncio.run(_notify())
        assert summary["skipped"] == "exception", "订阅层异常必须被兜成摘要"
        count += 1

        # ── 补充 4：阈值解析容错 ─────────────────────────────────────
        assert sc_notifier.parse_min_price(None) == 30, "缺省阈值是 30"
        count += 1
        assert sc_notifier.parse_min_price("  ") == 30, "空白阈值回落默认"
        count += 1
        assert sc_notifier.parse_min_price("30元") == 30, "非法阈值必须回落而不是崩"
        count += 1
        assert sc_notifier.parse_min_price("50") == 50, "合法阈值必须生效"
        count += 1
        assert sc_notifier.parse_min_price("-5") == 30, "负阈值回落默认"
        count += 1
        assert any("BILI_SC_MIN_PRICE 非法" in m for m in capture.messages), (
            "非法阈值必须留 warning 痕迹"
        )
        count += 1

    except AssertionError as exc:
        print(f"FAIL sc_notifier: {exc}")
        return 1
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK
        print(f"FAIL sc_notifier: 未预期异常 {type(exc).__name__}: {exc}")
        return 1
    finally:
        sc_notifier.sub_client = original_sub
        sc_notifier.dedup = original_dedup
        sc_notifier.sender = original_sender
        sc_notifier.NOTIFY_SC_ENABLED = original_enabled
        sc_notifier.SC_MIN_PRICE = original_min_price
        sc_notifier.LOGGER.removeHandler(capture)

    print(f"OK sc_notifier {count} 条断言")
    return 0


if __name__ == "__main__":
    sys.exit(main())
