# -*- coding: utf-8 -*-
# Copyright (C) 2026 小恩 <https://github.com/kmtshl-hub>
#
# This file is part of the douchong-notify extension for VR_douchong
#   (https://github.com/QianQiuZy/VR_douchong).
#
# Licensed under the GNU General Public License v2.0 only (GPL-2.0-only).
"""开源版本专属：渲染器 stub 的契约测试。

⚠️ 本仓库不包含图片卡片渲染器的完整实现（`dynamic_render.py` 是 stub）。
本文件守住「没有渲染器时也能正常工作」这一契约，防止将来有人把 stub 改成
抛异常 / 改返回值类型，导致通知静默丢失。
"""
from __future__ import annotations

import pathlib
import sys

SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from notify import dynamic_render  # noqa: E402


def test_stub_exposes_required_api() -> None:
    count = 0
    for name in ("render_dynamic", "render_live_start", "render_live_end", "_load_pillow"):
        assert hasattr(dynamic_render, name), f"stub 必须提供 {name}"
        count += 1
    print(f"  [ok] stub 提供 4 个必需符号（{count}/4）")


def test_stub_returns_none() -> None:
    count = 0
    assert dynamic_render.render_dynamic({"x": 1}) is None
    count += 1
    assert dynamic_render.render_live_start({"x": 1}, cover_bytes=None) is None
    count += 1
    assert dynamic_render.render_live_start({"x": 1}) is None  # 不传 cover_bytes 也必须可用
    count += 1
    assert dynamic_render.render_live_end({"x": 1}) is None
    count += 1
    print(f"  [ok] 三个渲染函数均返回 None（{count}/4）")


def test_stub_does_not_raise() -> None:
    count = 0
    # 调用方可能在数据不完整时调用 —— stub 不得抛异常，否则通知会被丢掉
    for payload in ({}, None, {"room_id": 1, "title": ""}, {"weird": object()}):
        dynamic_render.render_dynamic(payload)          # type: ignore[arg-type]
        dynamic_render.render_live_start(payload)       # type: ignore[arg-type]
        dynamic_render.render_live_end(payload)         # type: ignore[arg-type]
        count += 1
    print(f"  [ok] 异常输入不抛（{count}/4）")


def test_load_pillow_hook_is_callable() -> None:
    assert callable(dynamic_render._load_pillow)
    assert dynamic_render._load_pillow() is None
    print("  [ok] _load_pillow 可调用且返回 None（调用方/测试会引用该私有钩子）")


def main() -> int:
    print("stub 契约测试：")
    test_stub_exposes_required_api()
    test_stub_returns_none()
    test_stub_does_not_raise()
    test_load_pillow_hook_is_callable()
    print("OK: 4/4 stub contract checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
