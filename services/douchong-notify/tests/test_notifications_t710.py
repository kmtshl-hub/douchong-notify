# Standard-library-only assertion script for T-710 notification delivery marking
# and startup live-room connection priority. Run inside the app container.

import asyncio

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



class FakeNotifier:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def notify_live(self, room_id, **kwargs):
        self.calls.append((room_id, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


async def exercise_on(result=None, *, notifier_present=True, error=None, initial=False, initial_silent=False):
    marked = []
    notifier = FakeNotifier(result=result, error=error) if notifier_present else None

    original_info = notifications._info
    original_label = notifications._label
    original_live_notifier = notifications._live_notifier
    original_mark_notified = notifications._mark_notified
    original_initial_silent = notifications.INITIAL_SILENT
    original_live_info = notifications.runtime_state.LIVE_INFO
    original_current_sessions = notifications.runtime_state.CURRENT_SESSIONS
    try:
        async def fake_info(room_id):
            return {"anchor": "主播", "title": "标题", "area": "分区", "cover": ""}

        async def fake_mark(room_id):
            marked.append(room_id)

        notifications._info = fake_info
        notifications._label = lambda room_id: f"room-{room_id}"
        notifications._live_notifier = lambda: notifier
        notifications._mark_notified = fake_mark
        notifications.INITIAL_SILENT = initial_silent
        notifications.runtime_state.LIVE_INFO = {101: {"title": "标题", "live_time": "fp"}}
        notifications.runtime_state.CURRENT_SESSIONS = {101: 9001}
        notifications._live_session_ids.pop(101, None)
        await notifications._on(101, initial)
        return marked, notifier
    finally:
        notifications._info = original_info
        notifications._label = original_label
        notifications._live_notifier = original_live_notifier
        notifications._mark_notified = original_mark_notified
        notifications.INITIAL_SILENT = original_initial_silent
        notifications.runtime_state.LIVE_INFO = original_live_info
        notifications.runtime_state.CURRENT_SESSIONS = original_current_sessions
        notifications._live_session_ids.pop(101, None)


async def exercise_priority(rooms, live):
    started = []
    sleeps = []
    original_get_room_ids = bootstrap.room_config.get_room_ids
    original_startup_live_rooms = bootstrap.notifications.startup_live_rooms
    original_start_client = bootstrap.monitoring_jobs.start_client
    original_sleep = bootstrap.asyncio.sleep
    try:
        bootstrap.room_config.get_room_ids = lambda: list(rooms)
        bootstrap.notifications.startup_live_rooms = lambda: set(live)

        async def fake_start_client(room_id):
            started.append(room_id)

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        bootstrap.monitoring_jobs.start_client = fake_start_client
        bootstrap.asyncio.sleep = fake_sleep
        await bootstrap.run_priority_clients_loop()
        return started, sleeps
    finally:
        bootstrap.room_config.get_room_ids = original_get_room_ids
        bootstrap.notifications.startup_live_rooms = original_startup_live_rooms
        bootstrap.monitoring_jobs.start_client = original_start_client
        bootstrap.asyncio.sleep = original_sleep


async def main():
    marked, notifier = await exercise_on({"sent": True, "skipped": None})
    assert marked == [101]
    assert len(notifier.calls) == 1

    marked, notifier = await exercise_on({"sent": False, "skipped": "no_targets"})
    assert marked == []
    assert len(notifier.calls) == 1

    marked, notifier = await exercise_on({"sent": False, "skipped": "unavailable"})
    assert marked == []
    assert len(notifier.calls) == 1

    marked, notifier = await exercise_on({"sent": False, "skipped": "duplicate"})
    assert marked == [101]
    assert len(notifier.calls) == 1

    marked, notifier = await exercise_on(notifier_present=False)
    assert marked == []
    assert notifier is None

    try:
        await exercise_on(error=RuntimeError("send failed"))
        assert False, "notify_live exception must propagate"
    except RuntimeError as exc:
        assert str(exc) == "send failed"

    marked, notifier = await exercise_on(
        {"sent": True, "skipped": None}, initial=True, initial_silent=True
    )
    assert marked == []
    assert len(notifier.calls) == 0

    started, sleeps = await exercise_priority([1, 2, 3, 4, 5], {3, 1})
    assert started == [1, 3, 2, 4, 5]
    assert sleeps == [3, 3, 3, 3]
    assert len(started) == len(set(started)) == 5

    started, sleeps = await exercise_priority([1, 2, 3, 4, 5], set())
    assert started == [1, 2, 3, 4, 5]
    assert sleeps == [3, 3, 3, 3]

    started, sleeps = await exercise_priority([1, 2, 3, 4, 5], {3, 99})
    assert started == [3, 1, 2, 4, 5]
    assert 99 not in started
    assert sleeps == [3, 3, 3, 3]

    started, sleeps = await exercise_priority([], {1})
    assert started == []
    assert sleeps == []

    notifications._prewarm_live_time.clear()
    notifications._prewarm_live_time.update({7: "a", 8: "b"})
    live_copy = notifications.startup_live_rooms()
    assert live_copy == {7, 8}
    live_copy.clear()
    assert notifications.startup_live_rooms() == {7, 8}
    notifications._prewarm_live_time.clear()


if __name__ == "__main__":
    asyncio.run(main())
    print("T-710 assertions passed")
