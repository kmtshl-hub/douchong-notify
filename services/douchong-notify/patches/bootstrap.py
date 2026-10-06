# Copyright (C) 2026 小恩 <https://github.com/kmtshl-hub>
# Copyright (C) QianQiuZy (original work, VR_douchong)
#
# This file is a MODIFIED version of a file from VR_douchong
#   upstream: https://github.com/QianQiuZy/VR_douchong
#   original work Copyright (C) QianQiuZy, licensed under GPL-2.0
#
# Modifications by 小恩 <https://github.com/kmtshl-hub>; each change is
# marked with "[PATCH]" or described in an inline comment block below.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License only.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see
# <https://www.gnu.org/licenses/old-licenses/gpl-2.0.html>.
#
#   upstream file: app/bootstrap.py
#
"""Runtime bootstrap and startup orchestration.

Todo 5 makes ownership of ``MAIN_LOOP``, ``_run_in_main_loop``,
``_run_api_server``, and the launcher startup ordering explicit:

* ``MAIN_LOOP`` lives on :mod:`runtime_state` and is assigned to the
  running event loop by :func:`main` before any workers spin up.
* ``_run_in_main_loop`` is owned by :mod:`api_app` (used by the FastAPI
  routes to hop from the uvicorn thread to the main loop).
* ``_run_api_server`` (this module) constructs the uvicorn server bound to
  :attr:`api_app.app`.
* :func:`run` reproduces the pre-extraction ``__main__`` block: call
  :func:`create_schema`, :func:`ensure_runtime_schema`, spawn the API
  thread, and then ``asyncio.run(main())``.

The archive scheduler (:func:`monthly_reset_scheduler`) lives here because
it belongs to the runtime orchestration lane, invoking
:mod:`archive_service` at the appropriate times.
"""

# ══════════════════════════════════════════════════════════════════════════
# 本文件是上游 `app/bootstrap.py` 的**补丁版**（T-704）。
# 上游文件路径：app/bootstrap.py（容器内：/app/app/bootstrap.py）
# ══════════════════════════════════════════════════════════════════════════
# 本文件是上游 `app/bootstrap.py` 的**补丁版**（T-704 + T-706）。
# 上游文件路径：app/bootstrap.py（容器内：/app/app/bootstrap.py）
# 上游快照：239 行，sha256 = 81e7114ba4c002dfc6103e22a030c426c5e028f8cb116eca4fa55bd8fb75a234
#
# 相对上游**仅五处功能改动**（另加本说明头；其余逐字一致）：
#   ① 可选导入 `app.notify.dynamic_fetcher`；导入失败只记 warning 并跳过 worker。
#   ② `main()` 创建抓取 Task，并把该 Task 作为 `asyncio.gather(...)` 最后一个成员。
#   ③ `finally` 中先 cancel + await 抓取 Task，再执行既有 metrics/session 清理。
#   ④ T-706：UID 初始化后、首次状态轮询前恢复跨重启的已通知直播场次状态。
#   ⑤ T-710②：启动连接时优先连接预热判定为在播的受控房间；连接间隔仍为 3 秒。
#
# 上游升级时：先以新版 `app/bootstrap.py` 覆盖本文件，再按 ①→⑤ 重新应用；
#             复核抓取 Task 仍位于 gather 末尾，且 finally 先停抓取再关共享 session。
# ══════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from typing import Optional

from . import api_app, archive_service, monitoring_jobs, notifications, room_config, runtime_state
from .config import APP_HOST, APP_PORT
from .database import create_schema, ensure_runtime_schema, log_pool_status
from .metrics_runtime import flush_session
from .models import RoomInfo
from .whale_archive import archive_whale_month

try:
    from .notify import dynamic_fetcher
except Exception as exc:  # R-18 is optional and must never block core startup.
    dynamic_fetcher = None
    logging.warning(
        "[notify] dynamic fetcher unavailable; worker skipped error_type=%s",
        type(exc).__name__,
    )

POOL_STATUS_INTERVAL_SECONDS = 300


# ------------------ time helpers ------------------ #
def _now() -> datetime.datetime:
    return datetime.datetime.now()


async def _sleep_until(target: datetime.datetime) -> None:
    while True:
        remaining = (target - _now()).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(remaining)


def _month_str_now() -> str:
    from .repositories.tables import month_str

    return month_str()


# ------------------ archive scheduler ------------------ #
async def _archive_month(target_month: Optional[str] = None) -> None:
    """Run archive jobs serially, reporting failures before continuing."""
    for archive_job in (
        archive_service.archive_super_chat_log,
        archive_service.archive_room_live_stats,
        archive_service.archive_attention,
        archive_whale_month,
    ):
        try:
            await asyncio.to_thread(archive_job, target_month)
        except Exception as exc:
            logging.error(
                "[archive] job failed; continuing job=%s error_type=%s",
                archive_job.__name__,
                type(exc).__name__,
            )
    try:
        await asyncio.to_thread(archive_service.archive_live_session, target_month)
    except Exception as exc:
        logging.error(
            "[archive] job failed; continuing job=%s error_type=%s",
            archive_service.archive_live_session.__name__,
            type(exc).__name__,
        )
    log_pool_status("monthly_archive")


async def monthly_reset_scheduler() -> None:
    startup_now = _now()
    startup_month = _month_str_now()
    if startup_now.month == 12:
        first_target = startup_now.replace(
            year=startup_now.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    else:
        first_target = startup_now.replace(
            month=startup_now.month + 1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

    if (first_target - startup_now).total_seconds() <= 60:
        await _sleep_until(first_target)
        await _archive_month(startup_month)
        await _archive_month()
    else:
        await _archive_month()
        if _month_str_now() != startup_month:
            await _archive_month(startup_month)

    while True:
        now = _now()
        if now.month == 12:
            target = now.replace(
                year=now.year + 1,
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        else:
            target = now.replace(
                month=now.month + 1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

        await _sleep_until(target)
        previous_month = _month_str_now_at(target - datetime.timedelta(days=1))
        drift_seconds = max(0.0, (_now() - target).total_seconds())
        logging.info(
            f"[archive] 月切触发，month={previous_month} drift={drift_seconds:.3f}s"
        )
        await _archive_month(previous_month)


def _month_str_now_at(dt: datetime.datetime) -> str:
    from .repositories.tables import month_str

    return month_str(dt)


# ------------------ startup wiring ------------------ #
def init_room_info() -> None:
    """Seed RoomInfo anchor names from the configured rooms."""
    from . import room_config

    for room_id, name in room_config.get_room_anchors().items():
        RoomInfo.upsert(room_id, anchor_name=name)


def init_session() -> None:
    """Initialise the shared aiohttp session used by all Bilibili calls."""
    from . import bilibili_gateway

    bilibili_gateway.init_session()


def _flush_active_metrics(end_time: datetime.datetime) -> None:
    for room_id, session_id in tuple(runtime_state.CURRENT_SESSIONS.items()):
        monitoring_jobs.flush_pending_danmaku_for_room(room_id, session_id, end_time)
        flush_session(session_id, end_time)


async def pool_status_scheduler() -> None:
    while True:
        log_pool_status("periodic", level=logging.INFO)
        await asyncio.sleep(POOL_STATUS_INTERVAL_SECONDS)


# 与上游 run_clients_loop 保持一致：每连一个房间间隔 3 秒（避免同时连接触发 B站限流）。
# ⚠️ 若上游调整该间隔，这里需同步（见文件头「相对上游的功能改动」清单 ⑤）。
CONNECT_INTERVAL_SECONDS = 3


async def run_priority_clients_loop() -> None:
    """同上游 run_clients_loop，但**启动时在播的房间优先连接**。

    动机（2026-10-06 实测）：上游按 rooms.json 顺序串行连接、每房间间隔 3 秒，
    43 个房间 ⇒ 最后一个房间的采集中断达 148 秒；而「正在播」的房间才可能产生
    礼物/SC/上舰（**这些事件无补录路径**）⇒ 把它们排到最前，可把在播房间的中断
    从 ~127 秒降到 ~22~25 秒。

    降级保证：预热失败 ⇒ startup_live_rooms() 为空 ⇒ 顺序与上游完全一致。
    """
    all_rooms = room_config.get_room_ids()
    live = notifications.startup_live_rooms()
    if live:
        priority = [r for r in all_rooms if r in live] + [r for r in all_rooms if r not in live]
        logging.info(
            "[connect] 优先连接在播房间 %d 个（受控共 %d 个）",
            sum(1 for r in all_rooms if r in live),
            len(all_rooms),
        )
    else:
        priority = list(all_rooms)

    for index, room_id in enumerate(priority):
        await monitoring_jobs.start_client(room_id)
        if index + 1 < len(priority):
            await asyncio.sleep(CONNECT_INTERVAL_SECONDS)


# ------------------ main coroutine ------------------ #
async def main() -> None:
    """Top-level runtime coroutine.

    Assigns ``runtime_state.MAIN_LOOP`` (source of truth for the FastAPI
    thread bridge) and starts every worker gather.  The gather block is
    preserved verbatim from the pre-extraction launcher.
    """
    runtime_state.MAIN_LOOP = asyncio.get_running_loop()
    init_room_info()
    init_session()
    # 先初始化 UID + 粉丝数，完成后再开启状态轮询
    await monitoring_jobs.init_uids_and_attention_once()

    await notifications.restore_live_state()

    dynamic_fetcher_task = None
    if dynamic_fetcher is not None:
        dynamic_fetcher_task = asyncio.create_task(dynamic_fetcher.run_forever())

    try:
        await asyncio.gather(
            run_priority_clients_loop(),  # 在播房间优先连接（T-710②）
            monitoring_jobs.monitor_all_rooms_status(),  # 按 UID 批量轮询直播状态
            monthly_reset_scheduler(),
            monitoring_jobs.reconnect_scheduler(),  # 每日 6:00 全量重连
            monitoring_jobs.refresh_attention_scheduler(),  # 每 3 小时刷新关注数（attention）
            monitoring_jobs.attention_worker(),  # 粉丝数任务 worker（开播/下播+每日快照）
            monitoring_jobs.daily_attention_worker(),
            monitoring_jobs.attention_daily_scheduler(),
            monitoring_jobs.guard_daily_scheduler(),
            monitoring_jobs.fans_daily_scheduler(),
            monitoring_jobs.guard_fans_worker(),  # 守护 + 粉丝团队列 worker
            monitoring_jobs.daily_guard_worker(),
            monitoring_jobs.daily_fans_worker(),
            monitoring_jobs.guard_fans_refresh_scheduler(),  # 未开播房间每小时刷新守护 + 粉丝团
            monitoring_jobs.bili_ticket_scheduler(),  # 每日 5:00 刷新 bili_ticket
            monitoring_jobs.danmaku_flush_scheduler(),
            monitoring_jobs.concurrency_poll_scheduler(),  # 开播房间每 15 秒轮询同接
            pool_status_scheduler(),
            *([dynamic_fetcher_task] if dynamic_fetcher_task is not None else []),
        )
    finally:
        if dynamic_fetcher_task is not None and not dynamic_fetcher_task.done():
            dynamic_fetcher_task.cancel()
            try:
                await dynamic_fetcher_task
            except asyncio.CancelledError:
                pass
        _flush_active_metrics(_now())
        if runtime_state.aiohttp_session:
            await runtime_state.aiohttp_session.close()


# ------------------ API server thread ------------------ #
def _run_api_server() -> None:
    import uvicorn

    config = uvicorn.Config(api_app.app, host=APP_HOST, port=APP_PORT, log_level="info")
    server = uvicorn.Server(config)
    server.run()


# ------------------ launcher entry ------------------ #
def run() -> None:
    """Reproduce the pre-extraction ``__main__`` block launcher order."""
    create_schema()
    ensure_runtime_schema()
    threading.Thread(target=_run_api_server, daemon=True).start()
    asyncio.run(main())
