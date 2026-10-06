// Copyright (C) 2026 小恩 <https://github.com/kmtshl-hub>
//
// This file is part of qqbot-core, the query-side service for VR_douchong
//   (https://github.com/QianQiuZy/VR_douchong).
//
// Licensed under the GNU General Public License v2.0 only (GPL-2.0-only).
// See the LICENSE file in the repository root.
//

/**
 * 契约测试（开源版本）。
 *
 * ⚠️ 本仓库的 `session-report.ts` 是**纯文本简易实现**。原先针对「完整版式」
 * 的用例（各 Top 10 榜、最近 7 场流水等）随完整实现一并留在原项目中，不在本仓库。
 *
 * 这里守住的是**对外契约与正确性语义**，任何人改这个文件都必须让它继续通过：
 *   · 模块导出面齐全（调用方 index.ts / subscriptions.ts 依赖的 14 个符号）
 *   · 缺失值一律「未采集」，绝不渲染成 0 / NaN / undefined
 *   · 时间面向北京时间，脏值不抛异常
 *   · 场次结束的**三态**（已结束 / 直播中 / 脏数据）不可混淆
 *   · 报告函数返回非空文本且不抛异常
 */
import { describe, expect, it } from 'vitest';

import {
  avgText,
  buildMonthlyReport,
  buildReport,
  countText,
  dayText,
  numOrNull,
  oneLine,
  scLine,
  sessionTiming,
  sumRevenueOrNull,
  sumYuanText,
  textOrNotCollected,
  yuanText,
  type Rankings,
  type Session,
} from '../session-report.js';

const session = (over: Partial<Session> = {}): Session => ({
  session_id: 1,
  start_time: '2026-10-01 12:00:00',
  end_time: '2026-10-01 14:00:00',
  title: '测试场次',
  gift: 100,
  guard: 20,
  super_chat: 5,
  payer_count: 3,
  danmaku_count: 400,
  avg_concurrency: 12.5,
  max_concurrency: 30,
  ...over,
});

const noRanks: Rankings = { available: false };

describe('导出面（调用方依赖的符号必须齐全）', () => {
  it('14 个函数 + 类型全部可用', () => {
    for (const fn of [
      oneLine, numOrNull, countText, yuanText, avgText, sumYuanText,
      sumRevenueOrNull, textOrNotCollected, dayText, sessionTiming, scLine,
      buildReport, buildMonthlyReport,
    ]) {
      expect(typeof fn).toBe('function');
    }
  });
});

describe('oneLine：压单行 + 按码点截断', () => {
  it('换行折成空格，其余横向空白保留', () => {
    expect(oneLine('a\nb\r\nc', 20)).toBe('a b c');
    expect(oneLine('a  b', 20)).toBe('a  b');
  });
  it('按 Unicode 码点截断，不切坏代理对', () => {
    expect(oneLine('😀😀😀', 2)).toBe('😀😀…');
  });
  it('缺失值 → 空串（不输出 undefined）', () => {
    expect(oneLine(undefined, 10)).toBe('');
  });
});

describe('缺失值语义：一律「未采集」，绝不渲染成 0 / NaN', () => {
  it('numOrNull 把脏值全部归一为 null', () => {
    for (const v of [null, undefined, '', '  ', 'abc', NaN, Infinity, -Infinity]) {
      expect(numOrNull(v as never)).toBeNull();
    }
    expect(numOrNull('12.5')).toBe(12.5);
    expect(numOrNull(0)).toBe(0);
  });
  it('countText / yuanText / avgText：缺失 → 未采集', () => {
    expect(countText(null)).toBe('未采集');
    expect(yuanText(undefined)).toBe('未采集');
    expect(avgText('')).toBe('未采集');
  });
  it('yuanText 保留 1 位小数并带「元」', () => {
    expect(yuanText(12.34)).toBe('12.3 元');
  });
  it('三项收入任一缺失 → 合计整体「未采集」（不给假精确值）', () => {
    expect(sumYuanText([1, 2, 3])).toBe('6.0 元');
    expect(sumYuanText([1, null, 3])).toBe('未采集');
    expect(sumRevenueOrNull([1, 2])).toBe(3);
    expect(sumRevenueOrNull([1, ''])).toBeNull();
  });
  it('textOrNotCollected：非计数串原样保留，不当成缺失', () => {
    expect(textOrNotCollected('12:03:33')).toBe('12:03:33');
    expect(textOrNotCollected(null)).toBe('未采集');
    expect(textOrNotCollected(0)).toBe('未采集');
  });
  it('dayText：接受 YYYYMMDD 等形态，越界给未采集', () => {
    expect(dayText('20261005')).toBe('2026-10-05');
    expect(dayText('2026-10-05')).toBe('2026-10-05');
    expect(dayText('20261305')).toBe('未采集');
    expect(dayText('abc')).toBe('未采集');
  });
});

describe('时间：面向北京时间，脏值不抛异常', () => {
  it('UTC 串按北京时间展示（+8）', () => {
    expect(sessionTiming('2026-10-01 12:00:00', '2026-10-01 14:00:00').state).toBe('ended');
    expect(sessionTiming('2026-10-01 12:00:00', '2026-10-01 14:00:00').durationText)
      .toBe('2小时0分钟');
  });
  it('结束时间的**三态**不可混淆', () => {
    expect(sessionTiming('2026-10-01 12:00:00', '2026-10-01 14:00:00').state).toBe('ended');
    expect(sessionTiming('2026-10-01 12:00:00', null).state).toBe('live');
    expect(sessionTiming('2026-10-01 12:00:00', 'not-a-time').state).toBe('unknown');
    expect(sessionTiming('2026-10-01 12:00:00', 'not-a-time').durationText).toBe('未采集');
  });
  it('时间字段脏值不抛异常（Intl.format(NaN) 会抛 RangeError）', () => {
    expect(() => sessionTiming('', '')).not.toThrow();
    expect(sessionTiming('', '').durationText).toBe('未采集');
  });
});

describe('scLine：金额缺失给出「金额未采集」而不是 0.0 元', () => {
  it('缺 price 时明确说明未采集', () => {
    const line = scLine({ send_time: '2026-10-01 12:00:00', uname: '甲', message: 'hi' });
    expect(line).toContain('金额未采集');
    expect(line).toContain('甲');
  });
  it('有 price 时带 ¥ 且保留 1 位小数', () => {
    expect(scLine({ send_time: '2026-10-01 12:00:00', uname: '乙', price: 30, message: 'x' }))
      .toContain('¥30.0');
  });
});

describe('报告函数：返回非空文本、不抛异常（简易纯文本版）', () => {
  it('无场次时给出明确提示', () => {
    expect(buildReport('张三', [], noRanks)).toBe('张三：本月暂无已记录场次');
    expect(buildMonthlyReport('张三', [])).toBe('张三：本月暂无已记录场次');
  });
  it('有场次时输出核心字段', () => {
    const out = buildReport('张三', [session()], noRanks);
    expect(out).toContain('【张三 本场数据】');
    expect(out).toContain('总流水：');
    expect(out).toContain('时长：2小时0分钟');
    expect(out).not.toContain('undefined');
    expect(out).not.toContain('NaN');
  });
  it('月度报告含逐场与合计', () => {
    const out = buildMonthlyReport('张三', [session()]);
    expect(out).toContain('【张三 本月直播情况】');
    expect(out).toContain('【本月合计】');
    expect(out).not.toContain('undefined');
  });
  it('缺失收入项时合计显式标注未采集，不当 0 累加', () => {
    const out = buildMonthlyReport('张三', [session({ gift: undefined })]);
    expect(out).toContain('未采集');
    expect(out).not.toContain('NaN');
  });
});
