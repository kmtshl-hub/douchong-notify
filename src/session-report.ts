// Copyright (C) 2026 小恩 <https://github.com/kmtshl-hub>
//
// This file is part of qqbot-core, the query-side service for VR_douchong
//   (https://github.com/QianQiuZy/VR_douchong).
//
// This program is free software; you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation; version 2 of the License only.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program; if not, see
// <https://www.gnu.org/licenses/old-licenses/gpl-2.0.html>.
//
// ---------------------------------------------------------------------------
// NOTE ON COMMENTS
// Comments below occasionally cite the original project's internal review
// notes / decision ids (e.g. "决策 D-39", "评审 F1", "需求 R-09").  Those
// documents are not part of this repository; the comments are kept verbatim
// because the rationale they record is the reason the code is written this way.
// ---------------------------------------------------------------------------

export type Session = {
  session_id?: number; start_time: string; end_time: string | null; title: string;
  /**
   * ⚠️ 三项收入**声明为可选**（2026-10-05 评审 F1）：接口在字段漏采时不会返回它们，
   * 声明成必填会让 TypeScript 放行 `gift + 0 + super_chat` 这类"缺项当 0"的算法，
   * 而那正是产生"假精确值"的根源。声明可选后，所有读取点都被迫显式处理缺失。
   */
  gift?: number; guard?: number; super_chat?: number; payer_count?: number;
  danmaku_count?: number; blind_box_count?: number; blind_box_profit?: number;
  avg_concurrency?: number | null; max_concurrency?: number | null;
  start_attention?: number | null; end_attention?: number | null;
  start_guard_1?: number | null; end_guard_1?: number | null;
  start_guard_2?: number | null; end_guard_2?: number | null;
  start_guard_3?: number | null; end_guard_3?: number | null;
};
/**
 * ⚠️ 榜单条目的金额 / 计数同样**声明可选**（第四轮评审的"下一轮第一优先怀疑点"，
 * 本轮主动收口）：把类型写成必填，展示层就会放心地直接 `money(x.amount)`，
 * 一旦上游漏字段就输出 `0.0 元`——与 `#SC记录` 的 `price` 是同一个缺陷模式。
 */
export type Rankings = { available: boolean; reason?: string; since?: number; session_id?: number;
  contribution_since?: number; contribution_unattributed?: number;
  contribution_users?: { name: string; amount?: number; gift?: number; guard?: number; super_chat?: number }[];
  users?: { name: string; amount?: number }[]; gifts?: { name: string; count?: number; amount?: number }[] };
/** `price` 可选：接口漏采时不能变成 `0.0 元`（第四轮评审的阻断项）。 */
export type ScRow = { send_time: string; uname?: string; uid?: number; price?: number; message: string };

/**
 * SC / 弹幕这类用户可控文本：压成单行并截断。
 *
 * ⚠️ 只处理**换行类**字符（评审 N3）：
 * 早期版本用 `/\s+/gu`，会把正常的连续空格、制表符、全角空格 U+3000 全部压成一个 ASCII 空格，
 * 误伤正常内容；现在只把换行折成空格，其余横向空白原样保留。
 * 截断按 **Unicode 码点**切，避免 `slice()` 把 emoji 等代理对切成半个字符。
 * （注意：按码点 ≠ 按字素簇，ZWJ 组合 emoji 仍可能在组合边界被切开——这是既定取舍。）
 */
export function oneLine(text: unknown, max: number): string {
  const flat = String(text ?? '').replace(/[\r\n\u2028\u2029]+/gu, ' ').trim();
  const chars = Array.from(flat);
  return chars.length > max ? `${chars.slice(0, max).join('')}…` : flat;
}

/**
 * 纯粹的格式化：调用方**必须已确认这是有限数值**。
 *
 * ⚠️ 早期版本写的是 `Number(v ?? 0).toFixed(1)`——那个 `?? 0` 就是"把未采集合成 0"的
 * 病根：它让所有调用方都能安全地传缺失值，于是没人去处理缺失（评审 D-39）。
 * 现在它不再兜底，兜底责任交给 `yuanText()` / `amountText()` 这两个**唯一出口**。
 */
const money = (v: number) => Number(v).toFixed(1);

/**
 * 把接口值规范成"有限数值或 null"。
 * `null` / `undefined` / 空串 / 脏串 / NaN / Infinity 全部归一为 `null`，
 * 后续统一显示「未采集」，绝不让 `NaN` 或 `0.0` 漏到用户眼前（决策 D-39）。
 */
export function numOrNull(v: number | string | null | undefined): number | null {
  if (v == null) return null;
  if (typeof v === 'string' && v.trim() === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** 计数展示的唯一出口：缺失 / 脏值 → 「未采集」，绝不给 0（决策 D-39）。 */
export const countText = (v: number | string | null | undefined): string => {
  const n = numOrNull(v);
  return n == null ? '未采集' : String(n);
};

/** 金额展示的唯一出口（带「元」）：缺失 / 脏值 → 「未采集」（决策 D-39）。 */
export const yuanText = (v: number | string | null | undefined): string => {
  const n = numOrNull(v);
  return n == null ? '未采集' : `${money(n)} 元`;
};

/** 金额展示的唯一出口（不带「元」，用于 `¥` 前缀或表格内）。 */
export const amountText = (v: number | string | null | undefined): string => {
  const n = numOrNull(v);
  return n == null ? '未采集' : money(n);
};

/**
 * 仅用于**排序 / 过滤**的取值：缺失按 0 参与比较（未知项自然排最后、且不会"贡献"金额），
 * 但**展示时永远要走 `yuanText`**，不允许把这个 0 渲染给用户。
 */
const sortValue = (v: number | string | null | undefined): number => numOrNull(v) ?? 0;

/**
 * 三项收入（礼物 / 上舰 / SC）的合计文案——**唯一出口**。
 *
 * `gift + 0 + super_chat` 这种"缺项当 0"的算法会产出**假精确值**：看起来是完整合计，
 * 实际上少算了一整类收入。所以任一缺失就整体给「未采集」。
 *
 * ⚠️ 用 `numOrNull`（而不是 `== null`）逐个归一（第四轮评审的尾项）：
 * 只看 null/undefined 的话，接口若违反契约返回 `""` 会 `Number("") === 0` → 显示「0.0 元」，
 * 返回 `"null"` / `NaN` / `Infinity` 更会漏出 `NaN 元` / `Infinity 元`。
 */
export function sumYuanText(parts: ReadonlyArray<number | string | null | undefined>): string {
  const values = parts.map(numOrNull);
  if (values.some((value) => value == null)) return '未采集';
  return yuanText(values.reduce<number>((sum, value) => sum + (value ?? 0), 0));
}

/** 三项收入合计的**数值**（缺失 / 脏值 → null）。只给需要排序或判断的场景用。 */
export function sumRevenueOrNull(
  parts: ReadonlyArray<number | string | null | undefined>,
): number | null {
  const values = parts.map(numOrNull);
  if (values.some((value) => value == null)) return null;
  return values.reduce<number>((sum, value) => sum + (value ?? 0), 0);
}

/**
 * 「接口原样给的展示串」（时长、标题这类**非计数**字段）的唯一出口。
 *
 * ⚠️ 为什么不能复用 `countText()`：它内部走 `Number(v)`，`"12:03:33"` 会被
 * `Number()` 判成 `NaN` → 显示「未采集」，把**明明有数据**的字段说成没采集
 * （反向的静默错误，同样要不得）。
 * 也不能直接 `${v}` 插值：缺失时会输出字面量 `undefined`——需求 R-04 的通用验收
 * 标准明写「不得直接插值输出 `undefined`」。
 *
 * 非空字符串 → 压单行 + 按码点限长后原样返回；其余（缺失 / 非字符串 / 空白）→「未采集」。
 */
export function textOrNotCollected(value: unknown, max = 24): string {
  if (typeof value !== 'string') return '未采集';
  const flat = oneLine(value, max);
  return flat === '' ? '未采集' : flat;
}

/**
 * 「日期」展示的唯一出口。斗虫 `attention.date` 给的是 `YYYYMMDD` 裸串（如 `20261005`），
 * 直出既不美观，也与项目「时间统一出口、所有时间面向北京时间」（需求 R-10 通用条件）
 * 的约定不一致。
 *
 * 接受 `YYYYMMDD` / `YYYY-MM-DD` / `YYYY/MM/DD`，统一输出 `YYYY-MM-DD`；
 * 位数不对或月/日越界 →「未采集」（宁可说没采集，也不猜一个日期出来）。
 */
export function dayText(value: unknown): string {
  if (value == null) return '未采集';
  const digits = String(value).trim().replace(/[^0-9]/gu, '');
  if (digits.length !== 8) return '未采集';

  const year = digits.slice(0, 4);
  const month = digits.slice(4, 6);
  const day = digits.slice(6, 8);
  if (Number(month) < 1 || Number(month) > 12) return '未采集';
  if (Number(day) < 1 || Number(day) > 31) return '未采集';
  return `${year}-${month}-${day}`;
}

/**
 * 斗虫返回的是**无时区的 UTC 字符串**（采集容器按 UTC 写库）。
 *
 * ⚠️ 为什么不能直接 `Date.parse(...)` 就用（代码评审 P1-2）：
 * 字段格式异常（空串、带时区后缀、格式漂移）时 `Date.parse` 返回 `NaN`，
 * 而 `Intl.DateTimeFormat.format(NaN)` 会抛 `RangeError: Invalid time value`，
 * 整条命令退化成通用错误提示，让人分不清是"接口挂了"还是"某个字段脏了"。
 * 需求 R-09 要求缺数据显示"未采集"而非报错——所以这里统一返回 `null` 由调用方兜底。
 */
function parseUtc(value: unknown): number | null {
  if (typeof value !== 'string' || value.trim() === '') return null;
  const ms = Date.parse(value.replace(' ', 'T') + 'Z');
  return Number.isFinite(ms) ? ms : null;
}

const TIME_FORMAT = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
});

/**
 * 时间展示：解析不出来就给「未采集」，绝不把 NaN 交给 Intl，也不抛错。
 *
 * 导出给 `index.ts` 复用（评审 F3）：需求 R-10 的「所有时间均显示北京时间」是
 * **通用验收条件**，凡是把斗虫原始 UTC 串直接拼进回复的命令都算违反，
 * 所以 `#场次统计` / `#SC记录` 也走这里，而不是各自 `String(...)` 直出。
 */
export function showTime(value: unknown): string {
  const ms = parseUtc(value);
  return ms === null ? '未采集' : TIME_FORMAT.format(ms);
}

/** 毫秒时间戳展示（用于接口给的 epoch 秒），同样带范围与有限性防卫。 */
const showEpochSeconds = (value: unknown): string => {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return '未采集';
  const ms = seconds * 1000;
  // Intl 的可表示范围是 ±8.64e15 毫秒，超了照样抛 RangeError
  if (!Number.isFinite(ms) || Math.abs(ms) > 8.64e15) return '未采集';
  return TIME_FORMAT.format(ms);
};

/**
 * 场次的结束状态——**必须区分三种，不能混**（代码评审 N1）：
 *
 * | 情况 | 语义 | 时长 | 场次内 SC 归属 | 计入月度合计 |
 * |---|---|---|---|---|
 * | 有合法结束时间 | 已结束 | 起→止 | 有明确上界 | ✅ |
 * | `end_time` 缺失 / null / 空串 | **直播中** | 起→当前 | 上界 = 当前 | ✅（已播部分） |
 * | `end_time` 非空但解析不了 | **脏数据** | 未采集 | **不展示**（无法归属） | ❌ |
 *
 * 早期实现把后两者合并处理，导致「非法结束时间被按直播中算出一个精确时长」
 * （同一场次里同时出现"结束：未采集 + 时长 6小时30分钟"，自相矛盾），
 * 而真正的直播中场次又被月度合计排除——两头都不一致。
 */
type SessionEnd =
  | { kind: 'ended'; ms: number }
  | { kind: 'live'; ms: number }
  | { kind: 'unknown'; ms: null };

function sessionEnd(endTime: unknown, now: number): SessionEnd {
  if (endTime === null || endTime === undefined || endTime === '') return { kind: 'live', ms: now };
  const ms = parseUtc(endTime);
  return ms === null ? { kind: 'unknown', ms: null } : { kind: 'ended', ms };
}

/** 结束时间文案：已结束给时刻，直播中给「直播中」，脏数据给「未采集」。 */
const endText = (raw: unknown, end: SessionEnd): string =>
  end.kind === 'ended' ? showTime(raw) : end.kind === 'live' ? '直播中' : '未采集';

/** 时长文案：起点或终点任一无法确定 → 「未采集」（绝不猜）。 */
function durationText(startMs: number | null, end: SessionEnd): string {
  if (startMs === null || end.ms === null) return '未采集';
  const seconds = Math.max(0, Math.floor((end.ms - startMs) / 1000));
  return `${Math.floor(seconds / 3600)}小时${Math.floor(seconds % 3600 / 60)}分钟`;
}

/**
 * 单场时间的**三态**文案，供 `index.ts` 的 `#场次统计` 复用（评审 F3）。
 *
 * 之前 `#场次统计` 直接拼原始 UTC 串、且完全没有 R-04 要求的「时长」，
 * 与 `session-report.ts` 的三态语义也对不上。这里把口径收成一处，
 * 避免"同一个概念在两个文件里各写一遍、然后慢慢漂移"。
 */
export type SessionTiming = {
  startText: string;
  endText: string;
  durationText: string;
  state: 'ended' | 'live' | 'unknown';
};

export function sessionTiming(start: unknown, end: unknown, now = Date.now()): SessionTiming {
  const startMs = parseUtc(start);
  const resolved = sessionEnd(end, now);
  return {
    startText: showTime(start),
    endText: endText(end, resolved),
    durationText: durationText(startMs, resolved),
    state: resolved.kind,
  };
}

function change(a?: number | null, b?: number | null, live = false) {
  const from = numOrNull(a);
  const to = numOrNull(b);
  if (live && to == null) {
    return from == null ? '尚无快照' : `开播 ${countText(from)}（结束后对比）`;
  }
  if (from == null || to == null) return '未采集';
  const delta = to - from;
  return `${to}（${delta >= 0 ? '+' : ''}${delta}）`;
}

/**
 * 单场总流水：三项里**任一缺失就不能给精确值**（评审 F1）。
 * 例如只采到礼物、上舰缺失时，`gift + 0 + super_chat` 得到的"总流水"是假精确值。
 */
const totalOfSession = (s: Session): number | null => {
  const gift = numOrNull(s.gift);
  const guard = numOrNull(s.guard);
  const superChat = numOrNull(s.super_chat);
  if (gift == null || guard == null || superChat == null) return null;
  return gift + guard + superChat;
};

/** 单场总流水文案：任一收入项缺失 → 「未采集」（评审 F1）。导出给 `#场次统计` 复用。 */
export function sessionTotalText(s: Session): string {
  return yuanText(totalOfSession(s));
}

/**
 * 一条 SC 的展示文案——**唯一出口**，`#SC记录`（index.ts）与 `#查直播` 的「最近 SC」
 * 都调它（第四轮评审的阻断项）。
 *
 * 之前两个路径各写一份，且金额都写成 `money(Number(row.price ?? 0))`：
 * 接口漏掉 `price` 时会显示 `0.0 元`，群用户会当成"这条 SC 真的只值 0 元"。
 * 现在缺失统一给「金额未采集」，规则只有一处，改一次两个路径同时生效。
 */
export function scLine(row: ScRow): string {
  const who = row.uname || (row.uid == null ? '未知用户' : String(row.uid));
  const price = numOrNull(row.price);
  const amount = price == null ? '金额未采集' : `¥${money(price)}`;
  return `${showTime(row.send_time)} ${who} ${amount}：${oneLine(row.message, 80)}`;
}

/**
 * 同接（浮点均值）的展示：缺失 → 「未采集」，保留 1 位小数。
 * 用 numOrNull 而不是 `== null`，避免脏串直接进 `toFixed()` 抛错。
 */
export function avgText(v: number | string | null | undefined): string {
  const n = numOrNull(v);
  return n == null ? '未采集' : n.toFixed(1);
}

type Aggregate = { sum: number; missing: number; total: number };

/** 聚合时把「未采集」与「真实的 0」分开统计（评审 F1）。 */
function aggregate<T>(rows: readonly T[], pick: (row: T) => number | null | undefined): Aggregate {
  let sum = 0;
  let missing = 0;
  for (const row of rows) {
    const value = numOrNull(pick(row));
    if (value == null) {
      missing += 1;
      continue;
    }
    sum += value;
  }
  return { sum, missing, total: rows.length };
}

/**
 * 聚合文案（评审 F1）。
 *
 * 全部场次都有数据 → 精确合计；
 * 有缺项 → 给出「已采集部分合计」并**显式标注缺了几场**。
 * 绝不能把未采集当成 0 加进去：那样在"部分场次漏采"时，
 * 会把一个不完整的合计伪装成精确值——这比直接报"未采集"更有害。
 */
function aggregateText(agg: Aggregate, format: (value: number) => string): string {
  if (agg.missing === 0) return format(agg.sum);
  if (agg.missing === agg.total) return '未采集';
  return `${format(agg.sum)}（另有 ${agg.missing}/${agg.total} 场未采集，未计入）`;
}

/**
 * 「本场数据」报告 —— **开源版本自带的简易纯文本版**。
 *
 * ⚠️ 本仓库提供的是**简版**：核心字段 + 可选「最近 SC」。
 * 完整版式（综合贡献 / SC 贡献 / 上舰贡献 / 普通礼物贡献 各个 Top 10 榜、
 * 「未归属用户流水」、「综合榜自 … 启用起累计」说明、最近 7 场流水等）
 * 不在本开源仓库内。
 *
 * 调用方（index.ts）与完整版**签名一致**，替换实现不需要改调用点。
 */
export function buildReport(
  name: string,
  sessions: Session[],
  ranks: Rankings,
  scRows: ScRow[] = [],
  now = Date.now(),
): string {
  const rows = [...sessions].sort((a, b) => a.start_time.localeCompare(b.start_time));
  const s = rows.at(-1);
  if (!s) return `${name}：本月暂无已记录场次`;

  const startMs = parseUtc(s.start_time);
  const end = sessionEnd(s.end_time, now);
  const live = end.kind === 'live';

  // 起点或终点任一不确定 → 无法判断归属窗口，宁可不展示场次内 SC
  const currentSc =
    startMs === null || end.ms === null
      ? []
      : scRows.filter((x) => {
          const t = parseUtc(x.send_time);
          return t !== null && t >= startMs && t <= end.ms;
        });

  const lines = [
    `【${name} 本场数据】`,
    s.title || '标题未采集',
    `${showTime(s.start_time)} → ${endText(s.end_time, end)}`,
    `时长：${durationText(startMs, end)}`,
    `总流水：${sessionTotalText(s)}`,
    `礼物 ${yuanText(s.gift)}｜上舰 ${yuanText(s.guard)}｜SC ${yuanText(s.super_chat)}`,
    `付费人数：${countText(s.payer_count)}｜弹幕：${countText(s.danmaku_count)}`,
    `盲盒：${countText(s.blind_box_count)} 个｜盈亏：${yuanText(s.blind_box_profit)}`,
    `同接：平均 ${avgText(s.avg_concurrency)}｜峰值 ${countText(s.max_concurrency)}`,
    `粉丝：${change(s.start_attention, s.end_attention, live)}`,
    `舰长 ${change(s.start_guard_1, s.end_guard_1, live)}｜提督 ${change(s.start_guard_2, s.end_guard_2, live)}｜总督 ${change(s.start_guard_3, s.end_guard_3, live)}`,
  ];

  if (currentSc.length) {
    lines.push('【最近 SC】', ...currentSc.slice(-5).reverse().map(scLine));
  }

  if (ranks.available && ranks.session_id === s.session_id) {
    const users = (ranks.users ?? [])
      .slice()
      .sort((a, b) => sortValue(b.amount) - sortValue(a.amount))
      .slice(0, 10);
    lines.push(
      '【普通礼物贡献 Top 10】',
      ...(users.length ? users.map((x, i) => `${i + 1}. ${x.name} ${yuanText(x.amount)}`) : ['暂无短期礼物榜数据']),
    );
  } else {
    lines.push('礼物榜单：本场暂无短期汇总（不会补造历史数据）');
  }

  return lines.join('\n');
}

/**
 * 「本月直播情况」报告 —— **开源版本自带的简易纯文本版**。
 *
 * 逐场简表 + 本月合计。合计一律走 `aggregateText`：
 * 缺项显式标注「另有 N/M 场未采集，未计入」，**绝不把未采集当 0**。
 * （完整版式的差异见 `buildReport` 的说明。）
 */
export function buildMonthlyReport(name: string, sessions: Session[], now = Date.now()): string {
  const rows = [...sessions].sort((a, b) => a.start_time.localeCompare(b.start_time));
  if (!rows.length) return `${name}：本月暂无已记录场次`;

  const lines = [`【${name} 本月直播情况】`, `场次：${rows.length}`, ''];
  rows.forEach((s, i) => {
    const startMs = parseUtc(s.start_time);
    const end = sessionEnd(s.end_time, now);
    lines.push(
      `${i + 1}. ${showTime(s.start_time)} → ${endText(s.end_time, end)}`,
      `   时长 ${durationText(startMs, end)}｜弹幕 ${countText(s.danmaku_count)}｜同接 ${avgText(s.avg_concurrency)}/${countText(s.max_concurrency)}`,
      `   礼物 ${yuanText(s.gift)}｜上舰 ${yuanText(s.guard)}｜SC ${yuanText(s.super_chat)}｜总计 ${sessionTotalText(s)}`,
      `   标题：${s.title || '未采集'}`,
    );
  });

  // 合计只累加「起点可解析 且 终点可确定」的场次：已结束用起止差，直播中用已播时长，脏数据排除
  const secondsAgg = aggregate(rows, (s) => {
    const startMs = parseUtc(s.start_time);
    if (startMs === null) return null;
    const end = sessionEnd(s.end_time, now);
    if (end.ms === null) return null;
    return Math.max(0, Math.floor((end.ms - startMs) / 1000));
  });
  const durationAgg =
    secondsAgg.missing === 0
      ? `${Math.floor(secondsAgg.sum / 3600)}小时${Math.floor((secondsAgg.sum % 3600) / 60)}分钟`
      : `${Math.floor(secondsAgg.sum / 3600)}小时${Math.floor((secondsAgg.sum % 3600) / 60)}分钟（另有 ${secondsAgg.missing}/${secondsAgg.total} 场时间无法确定，未计入）`;

  lines.push(
    '',
    '【本月合计】',
    `时长：${durationAgg}`,
    `弹幕：${aggregateText(aggregate(rows, (s) => s.danmaku_count), String)}`,
    `礼物 ${aggregateText(aggregate(rows, (s) => s.gift), yuanText)}｜上舰 ${aggregateText(aggregate(rows, (s) => s.guard), yuanText)}｜SC ${aggregateText(aggregate(rows, (s) => s.super_chat), yuanText)}｜总计 ${aggregateText(aggregate(rows, (s) => totalOfSession(s)), yuanText)}`,
  );
  return lines.join('\n');
}
