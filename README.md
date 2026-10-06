# douchong-notify

B站直播间 **QQ 群通知与数据查询**扩展 —— 基于 [VR_douchong](https://github.com/QianQiuZy/VR_douchong) 的派生项目。

---

## 基于 / Based on

本项目是 **[QianQiuZy](https://github.com/QianQiuZy) 的
[VR_douchong（VR斗虫自搭版）](https://github.com/QianQiuZy/VR_douchong)** 的衍生扩展，
**而非上游本体**。

具体地说，本项目以补丁形式覆盖上游的若干文件
（`app/bootstrap.py`、`app/notifications.py`、`app/event_ingestion.py`、`app/danmaku_metrics.py`），
并向上游进程内注入通知模块（`app/notify/`），以实现：

- QQ 群**开播 / 下播 / 动态 / SC / 进房**通知
- QQ 群**直播数据查询**（`#查直播`、`#本月统计`、`#词云`、`#表情排行` 等）

**上游才是这套系统的主体**，本仓库不能脱离上游独立运行。
如果你觉得这个项目对你有用，**请优先前往上游仓库点个 Star、关注 QianQiuZy 的工作**，
这是对原作者最直接的支持：

> 👉 **<https://github.com/QianQiuZy/VR_douchong>**

本项目遵循与上游相同的协议：**GNU GPL v2.0**（`GPL-2.0-only`）。
本项目由 [小恩](https://github.com/kmtshl-hub) 维护。

---

> This project is a derivative extension of
> [VR_douchong](https://github.com/QianQiuZy/VR_douchong) by
> [QianQiuZy](https://github.com/QianQiuZy). It is **not** the upstream
> project itself and **cannot run without it**. Please support the original
> author first. Licensed under GPL-2.0-only.
> Maintained by [小恩](https://github.com/kmtshl-hub).

---

## 一、这是什么

上游 VR_douchong 负责**采集**B站直播数据（礼物 / 上舰 / SC / 弹幕 / 同接）并存进 MySQL + Redis，
对外提供只读 HTTP API。它本身**不负责通知与查询**。

本项目补上这两块：

| 组成 | 作用 | 运行方式 |
|---|---|---|
| **补丁层**（`patches/`） | 把通知能力接进上游的启动流程与事件管道 | 以只读挂载**覆盖**上游同名文件 |
| **通知层**（`notify/`） | 开播/下播/动态/SC/进房通知的编排与发送 | 放进上游 `app/notify/`，在**上游进程内**运行 |
| **查询服务**（`src/`） | QQ 机器人：解析 `#` 指令、匹配主播、查询上游 API、格式化回复 | **独立进程**（Node.js） |
| **弹幕指标**（`services/douchong-report/`） | 弹幕词频 / 表情排行的聚合与读取 | 以只读挂载覆盖上游同名文件 |

技术上：

```
B站 ──► VR_douchong（Python：采集 + HTTP API）
                    ▲                    ▲
        补丁/通知模块（本仓库）           │ 只读 HTTP
                    │                    │
        SnowLuma（OneBot v11）◄── 本仓库的 QQ 机器人（Node.js）
```

**本项目不采集数据、不直连数据库**，只读上游 API。

---

## 二、这个开源版本包含什么 / 不包含什么

### ✅ 包含（可直接跑）

| 功能 | 状态 |
|---|---|
| 房间订阅 / 取消订阅 | ✅ |
| 开播 / 下播通知（QQ 群，**纯文本**） | ✅ |
| B站动态通知（QQ 群，**纯文本 + 链接**） | ✅ |
| SC（醒目留言）通知 | ✅ |
| 进房通知 | ✅ |
| Redis 去重、跨重启状态恢复 | ✅ |
| 弹幕词频 / 表情排行采集与查询 | ✅ |
| QQ 查询指令（`#查直播`、`#本月统计`、`#词云`、`#表情排行` 等） | ✅（**简版报告**，见下） |

### ❌ 不包含（有意省略）

| 省略项 | 说明 |
|---|---|
| **图片卡片渲染器** | `notify/dynamic_render.py` 在本仓库是 **stub**（返回 `None`），通知自动降级为纯文本。完整渲染器（仿手机端 B站动态卡片图）不在本仓库 |
| **完整版式的文字报告** | 本仓库 `src/session-report.ts` 提供**简易纯文本版**；完整版式（综合贡献 / SC / 上舰 / 普通礼物各 Top 10 榜、最近 7 场流水等）不在本仓库 |
| **字体素材** | `notify/assets/` 未包含（随渲染器一起省略） |

**设计说明**：这是常见的「**引擎开源 + 自带简易实现**」模式。
被省略的部分**不影响基础可用性** —— 所有调用点都已做优雅降级，
没有渲染器和精排版时，通知与查询**照常工作**，只是输出朴素一些。

需要更精致的输出，可以自己实现，或参见上游与本项目原作者的版本。

---

## 三、部署：文件映射表

本仓库的目录结构与上游**不是**一一对应，部署时需要按下表放置。

| 本仓库路径 | 放到上游的哪里 | 方式 |
|---|---|---|
| `services/douchong-notify/patches/bootstrap.py` | `app/bootstrap.py` | **覆盖**（只读挂载） |
| `services/douchong-notify/patches/notifications.py` | `app/notifications.py` | **覆盖** |
| `services/douchong-notify/patches/event_ingestion.py` | `app/event_ingestion.py` | **覆盖** |
| `services/douchong-report/danmaku_metrics.py` | `app/danmaku_metrics.py` | **覆盖** |
| `services/douchong-notify/notify/`（整个目录） | `app/notify/` | **新增**（上游原本没有该目录） |
| `src/` + `package.json` 等 | **不放进上游** | 作为**独立 Node 进程**运行 |

> ⚠️ 覆盖型文件请用**只读挂载**（`:ro`）而不是直接改写上游源码 ——
> 这样上游升级时只需重新应用补丁，不会污染上游工作树。

---

## 四、怎么跑起来

### 前置

1. 先按上游文档把 **VR_douchong** 跑起来（MySQL + Redis + `.env`），确认 API 可用
2. 一个 **OneBot v11** 实现（本项目的查询服务默认对接 SnowLuma，见 `SNOWLUMA_WS_URL`）
3. Node.js ≥ 22（跑查询服务）

### 步骤

```bash
# ① 取上游
git clone https://github.com/QianQiuZy/VR_douchong
cd VR_douchong
pip install -r requirements.txt
#   按上游 README 配置 .env（数据库 / B站 Cookies 等）并启动上游

# ② 应用本项目的补丁层（只读挂载的方式见 docker-compose 示例；手工验证可先直接复制）
cp <本仓库>/services/douchong-notify/patches/bootstrap.py       app/bootstrap.py
cp <本仓库>/services/douchong-notify/patches/notifications.py   app/notifications.py
cp <本仓库>/services/douchong-notify/patches/event_ingestion.py app/event_ingestion.py
cp <本仓库>/services/douchong-report/danmaku_metrics.py         app/danmaku_metrics.py
cp -r <本仓库>/services/douchong-notify/notify/                 app/notify/

# ③ 重启上游进程，让补丁与通知层生效

# ④ 跑查询服务（独立进程）
cd <本仓库>
npm install
cp .env.example .env     # 按需填写；凭据不要提交进 git
npm run dev
```

### 通知层需要的环境变量（示例）

```bash
# 群白名单：留空 = 不响应任何群（fail closed）。多个用逗号分隔
BILI_NOTIFY_GROUPS=
# 是否启用通知
BILI_NOTIFY_ENABLED=1
# 启动后是否静默（避免重启时补发历史通知）
BILI_NOTIFY_INITIAL_SILENT=1
# OneBot HTTP 接口
ONEBOT_HTTP_URL=http://127.0.0.1:3000
```

---

## 五、开发与验证

```bash
# ① TypeScript：类型检查 + 测试（6 个文件 / 84 个用例）
npm install
npm run verify

# ② Python：可独立运行的测试（脚本式，每个文件自带 main()）
python3 services/douchong-notify/tests/test_render_stub_contract.py
python3 services/douchong-notify/tests/test_live_notifier.py
python3 services/douchong-notify/tests/test_dynamic_notifier.py
#   …… 其余同理；补丁层的两个集成测试需要上游 app 包，缺失时会自动 SKIP

# 也可用 pytest 收集（脚本式测试已在 conftest.py 中排除，避免误报）
python3 -m pytest services/douchong-notify/tests/ -q

# ③ Python lint（与上游同一工具）
pip install -r requirements-dev.txt
ruff check .
```

### 关于 ruff 规则集

本仓库的 `ruff.toml` **显式声明**了规则集，而不是依赖出厂默认 —— 因为不同版本的
ruff 默认规则集不同（同一份代码「用默认」与「用 E4/E7/E9/F」的结论相差 70 项），
不写死会导致「lint 是否通过」随版本漂移。

其中被忽略的项都**逐条给了理由**，主要是两类：

1. **上游改写版文件**（`patches/*.py`、`danmaku_metrics.py`）：它们除改动点外
   **逐字保留上游内容**，以便上游升级时逐行比对合并 —— 所以它们携带的上游原有
   风格与遗留告警**不做修改**（改了反而破坏「可与上游 diff」这一性质）。
2. **脚本式测试的 `E402`**：需要在 `import` 之前插入 `sys.path`。

`F` 类（未定义名、未使用导入/变量等**真缺陷**）全部保持启用。

### 与上游的一致性

上游自带 `ruff` 与 `pytest`（其 README 记录本地验证 324 passed）。
本项目的补丁层改动**不应破坏上游既有测试**：

```bash
cd VR_douchong
ruff check .
pytest -q
```

> 📌 本仓库的代码注释中偶尔会引用原项目内部的评审编号 / 决策编号
> （例如「评审 F1」「决策 D-39」「需求 R-09」）。那些文档不在本仓库，
> 注释**原样保留**，因为它们记录的正是这些代码为何这样写的理由。

---

## 六、许可

**GNU General Public License, Version 2 only**（`SPDX: GPL-2.0-only`）。

Copyright (C) 2026 小恩 <https://github.com/kmtshl-hub>

- 全文见 [`LICENSE`](./LICENSE)
- 第三方归属与修改说明见 [`NOTICE`](./NOTICE)

由于本项目是 VR_douchong 的衍生作品，依据 GPL-2.0 §2：
**任何人分发本项目的修改版时，必须同样以 GPL-2.0 提供完整源码。**

```
This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; version 2 of the License only.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
```
