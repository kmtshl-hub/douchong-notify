# Copyright (C) 2026 小恩 <https://github.com/kmtshl-hub>
#
# This file is part of the douchong-notify extension for VR_douchong
#   (https://github.com/QianQiuZy/VR_douchong).
#
# Licensed under the GNU General Public License v2.0 only (GPL-2.0-only).
"""pytest 配置。

本项目 Python 测试的**主流形态是「脚本式」**：每个文件自带 `main()` 运行器，
用 `python3 <file>.py` 直接执行并打印 `N 条断言` / `OK: ...`。

`test_danmaku_metrics_t711.py` 属于此类，且它的步骤函数形如
`def test_xxx(module):` —— 其中 `module` 是 **`main()` 注入的已编译模块对象**，
**不是 pytest fixture**。pytest 收集它时报 `fixture 'module' not found`（9 个 error），
而该文件用 `python3` 直接跑是 **9/9 通过** 的。

⇒ 这里将它与 pytest 收集分离，两种跑法各司其职：

    python3 services/douchong-notify/tests/test_danmaku_metrics_t711.py   # 9/9 通过
    pytest  services/douchong-notify/tests/                               # 干净通过
"""

collect_ignore = [
    # 脚本式测试：由自带 main() 运行，不适用 pytest 收集
    "test_danmaku_metrics_t711.py",
]
