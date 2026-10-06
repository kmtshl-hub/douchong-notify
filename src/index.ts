import "dotenv/config";

import { SnowLumaWebSocketClient, text } from "@snowluma/sdk";

import {
  parseWatchedUserInput,
  SubscriptionStore,
  type NotifyFlags,
} from "./subscriptions.js";
import { startNotifyApi } from "./notify-api.js";
import { selectRooms } from "./room-selector.js";
import { resolvePublicRoom, parseNumericRoomQuery } from "./room-resolver.js";
import {
  avgText,
  buildMonthlyReport,
  buildReport,
  countText,
  dayText,
  numOrNull,
  scLine,
  sumRevenueOrNull,
  sumYuanText,
  sessionTiming,
  sessionTotalText,
  textOrNotCollected,
  yuanText,
  type Rankings,
  type ScRow,
  type Session,
} from "./session-report.js";

type RoomSummary = {
  room_id: number;
  anchor_name: string;
  status: number;
  gift: number;
  guard: number;
  super_chat: number;
  payer_count: number;
  fans_count: number;
  current_concurrency: number | null;
  // 接口给的 HH:MM:SS 展示串。声明成可选：斗虫没采集时不应让
  // `${live_duration}` 直出 `undefined`（需求 R-04 通用验收标准）。
  live_duration?: string;
  blind_box_count?: number;
  blind_box_profit?: number;
  guard_1?: number | null;
  guard_2?: number | null;
  guard_3?: number | null;
};

type AttentionRow = {
  date: string;
  attention: string;
  fans_count: number | null;
  guard_1: number | null;
  guard_2: number | null;
  guard_3: number | null;
  gift: number;
  guard: number;
  super_chat: number;
};

type DanmakuMetrics = {
  available: boolean;
  reason?: string;
  words?: Array<{ word: string; count: number }>;
  emotes?: Array<{ name: string; count: number }>;
};

type ReplyContext = {
  reply(message: ReturnType<typeof text>): Promise<unknown>;
};

type AnchorTarget = {
  roomId: number;
  name: string;
};

const wsUrl = process.env.SNOWLUMA_WS_URL ?? "ws://127.0.0.1:3001/";
const wsToken = process.env.SNOWLUMA_WS_TOKEN;

if (!wsToken) {
  throw new Error("SNOWLUMA_WS_TOKEN is required");
}

const biliApi = (
  process.env.BILI_DOUCHONG_API ?? "http://127.0.0.1:14666"
).replace(/\/+$/u, "");

function parseCsvIds(value: string | undefined): string[] {
  if (!value) {
    return [];
  }

  return [
    ...new Set(
      value
        .split(",")
        .map((item) => item.trim())
        .filter((item) => /^\d+$/u.test(item)),
    ),
  ];
}

const allowedGroups = new Set(parseCsvIds(process.env.BOT_ALLOWED_GROUPS));
const envAdminIds = parseCsvIds(process.env.BOT_ADMIN_IDS);

if (allowedGroups.size === 0) {
  console.log(
    JSON.stringify({
      type: "config_warning",
      reason: "BOT_ALLOWED_GROUPS_empty",
    }),
  );
}

const subscriptionsFile =
  process.env.SUBSCRIPTIONS_FILE ?? "data/subscriptions.json";

const store = new SubscriptionStore({
  filePath: subscriptionsFile,
  bootstrapAdmins: envAdminIds,
});

try {
  await store.load();
} catch (error: unknown) {
  console.log(
    JSON.stringify({
      type: "subscriptions_load_error",
      error: errorMessage(error),
    }),
  );
}

/**
 * ⚠️ 必须把「是否装载成功」写进日志（2026-10-05 代码评审 P1-5）：
 * 配置损坏时机器人外表照常在线，但所有管理命令会静默返回"配置损坏"。
 * 旧日志只打 `groups:0`，看起来跟"确实没有订阅"一模一样，运维根本发现不了。
 * 现在显式区分 `loaded` / `damaged`，容器日志一眼可见。
 */
console.log(
  JSON.stringify({
    type: "subscriptions_loaded",
    loaded: store.loaded,
    damaged: !store.loaded,
    groups: store.loaded ? Object.keys(store.snapshot().groups).length : 0,
  }),
);

const bot = new SnowLumaWebSocketClient({
  url: wsUrl,
  accessToken: wsToken,
  reconnect: true,
});

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }

  if (typeof error === "string") {
    return error;
  }

  return "unknown error";
}

async function replyText(ctx: ReplyContext, message: string): Promise<void> {
  await ctx.reply(text(message));
}

async function biliGet<T>(path: string): Promise<T> {
  const response = await fetch(`${biliApi}${path}`, {
    method: "GET",
    signal: AbortSignal.timeout(8_000),
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  return (await response.json()) as T;
}

/**
 * ⚠️ 这里**不再有本地的金额格式化函数**（第四轮评审的尾项）。
 *
 * 评审指出：我说"金额只有一个出口"，但 `index.ts` 自己还留着一套 `money()` /
 * `moneyOrNotCollected()` / `roomRevenueText()`——那就不叫唯一出口了。
 * 现在全部改走 `session-report.ts` 的 `yuanText()` / `sumYuanText()`，
 * 本文件里不再有任何金额格式化代码。
 *
 * ⚠️ 下面这个 `totalRevenue()` 只用于**排序**（未知项按 0 比较、排末位），
 * **绝不允许把它渲染给用户**——展示一律走 `sumYuanText()`（决策 D-47）。
 * 它用 numOrNull 逐个归一，避免空串 / 脏串被 Number() 悄悄变成 0 后参与排序。
 */
function totalRevenueForSort(room: RoomSummary): number {
  return [room.gift, room.guard, room.super_chat].reduce<number>(
    (sum, value) => sum + (numOrNull(value) ?? 0),
    0,
  );
}

function roomName(room: RoomSummary): string {
  return room.anchor_name || String(room.room_id);
}

async function resolveAnchorTarget(
  ctx: ReplyContext,
  query: string,
  notFoundMessage = "没有匹配到已监控主播，请换一个关键词",
): Promise<AnchorTarget | null> {
  const rooms = await biliGet<RoomSummary[]>("/gift");
  const matches = selectRooms(rooms, query);

  if (matches.length === 0) {
    // 只认 ASCII 纯数字（判定抽成纯函数，见 room-resolver.parseNumericRoomQuery）
    const numericRoomId = parseNumericRoomQuery(query);
    if (numericRoomId !== null) {
      const fallback = await resolvePublicRoom(numericRoomId);
      if (fallback !== null) {
        return { roomId: fallback.roomId, name: fallback.name };
      }
    }

    await replyText(ctx, notFoundMessage);
    return null;
  }

  if (matches.length === 1) {
    const room = matches[0];
    if (!room) {
      return null;
    }

    return {
      roomId: room.room_id,
      name: roomName(room),
    };
  }

  const shown = matches.slice(0, 15);
  const lines = [
    "匹配到多个主播，请输入更完整的名字：",
    ...shown.map(
      (room) => `• ${roomName(room)}（${room.room_id}）`,
    ),
  ];

  if (matches.length > shown.length) {
    lines.push(
      `另有 ${matches.length - shown.length} 个匹配，请缩小关键词范围`,
    );
  }

  await replyText(ctx, lines.join("\n"));
  return null;
}

async function resolveRoom(
  ctx: ReplyContext,
  query: string,
): Promise<number | null> {
  const target = await resolveAnchorTarget(ctx, query);
  return target?.roomId ?? null;
}

async function getRoomSummary(
  roomId: number,
  monthly = false,
): Promise<RoomSummary | null> {
  const rooms = await biliGet<RoomSummary[]>(
    monthly ? "/gift/by_month" : "/gift",
  );

  return rooms.find((room) => room.room_id === roomId) ?? null;
}

async function getSessions(roomId: number): Promise<Session[]> {
  const result = await biliGet<{ sessions: Session[] }>(
    `/gift/live_sessions?room_id=${encodeURIComponent(String(roomId))}`,
  );
  return result.sessions;
}

async function getScRows(roomId: number): Promise<ScRow[]> {
  const result = await biliGet<{ list: ScRow[] }>(
    `/gift/sc?room_id=${encodeURIComponent(String(roomId))}`,
  );
  return result.list;
}

async function getRankings(roomId: number): Promise<Rankings> {
  return biliGet<Rankings>(
    `/gift/report_rankings?room_id=${encodeURIComponent(String(roomId))}`,
  );
}

/** 参数为空时回提示并返回 null；非空则返回 trim 后的参数。统一返回 Promise，避免调用处漏 await。 */
async function requireArgument(
  ctx: ReplyContext,
  query: string | undefined,
  message: string,
): Promise<string | null> {
  const trimmed = query?.trim() ?? "";
  if (trimmed.length > 0) {
    return trimmed;
  }

  await replyText(ctx, message);
  return null;
}

function parseNotifySettings(
  input: string,
):
  | { ok: true; keyword: string; patch: Partial<NotifyFlags> }
  | { ok: false; reason: "keyword" | "format" } {
  const tokens = input.trim().split(/\s+/u).filter(Boolean);
  const keywordTokens: string[] = [];
  const patch: Partial<NotifyFlags> = {};
  let switchCount = 0;

  const allowedKeys = new Set<keyof NotifyFlags>([
    "dynamic",
    "live",
    "liveEnd",
    "sc",
    "atAll",
  ]);

  for (const token of tokens) {
    if (!token.includes("=")) {
      keywordTokens.push(token);
      continue;
    }

    const equalIndex = token.indexOf("=");
    const rawKey = token.slice(0, equalIndex);
    const rawValue = token.slice(equalIndex + 1);

    const key = (
      rawKey === "dynamic"
        ? "dynamic"
        : rawKey === "live"
          ? "live"
          : rawKey === "liveEnd"
            ? "liveEnd"
            : rawKey === "sc"
              ? "sc"
              : rawKey === "atAll"
                ? "atAll"
                : null
    ) satisfies keyof NotifyFlags | null;

    if (
      key === null ||
      !allowedKeys.has(key) ||
      !/^(?:on|off)$/iu.test(rawValue)
    ) {
      return { ok: false, reason: "format" };
    }

    patch[key] = rawValue.toLowerCase() === "on";
    switchCount += 1;
  }

  const keyword = keywordTokens.join(" ").trim();

  if (!keyword) {
    return { ok: false, reason: "keyword" };
  }

  if (switchCount === 0) {
    return { ok: false, reason: "format" };
  }

  return { ok: true, keyword, patch };
}

function helpText(): string {
  return [
    "可用命令：",
    "#ping",
    "#状态",
    "#斗虫汇总",
    "#营收排行",
    "#直播状态",
    "#本月统计 <关键词>",
    "#查直播 <关键词>",
    "#本场数据 <关键词>",
    "#房间统计 <关键词>",
    "#场次统计 <关键词>",
    "#SC记录 <关键词>",
    "#粉丝快照 <关键词>",
    "#词云 <关键词>",
    "#表情排行 <关键词>",
    "#订阅 <关键词或房间号>",
    "#退订 <关键词或房间号>",
    "#订阅列表",
    "#通知设置 <关键词> <开关...>",
    "#特关添加 <UID> [显示名]",
    "#特关移除 <UID>",
    "#特关列表",
    "#管理员",
    "#帮助",
  ].join("\n");
}

async function rejectIfAnchorUnmonitored(
  ctx: ReplyContext,
  roomId: number,
): Promise<boolean> {
  const room = await getRoomSummary(roomId);
  if (room !== null) {
    return false;
  }

  await replyText(ctx, "该主播未被监控，无法开启通知");
  return true;
}

function isAdmin(groupId: string, userId: string): boolean {
  return store.isAdmin(groupId, userId);
}

/**
 * 订阅配置损坏时的守卫（代码评审 P1-5 的用户侧那一半）。
 *
 * ⚠️ 为什么需要它：启动日志加了 `damaged` 只是让**运维**看得见；
 * 但群里的管理员仍然会收到误导性回复——
 *   ・`#订阅列表` 会被告知「本群暂无主播订阅」（其实是读不到，不是没有）；
 *   ・只登记在损坏文件里的管理员会先被 `rejectNonAdmin` 判成「没有权限」。
 * 两种都像是"正常业务结果"，用户只会以为机器人坏了又查不出原因。
 * 所以管理命令的**第一步**先看装载状态，坏了就直说。
 */
async function rejectIfStoreDamaged(ctx: ReplyContext): Promise<boolean> {
  if (store.loaded) {
    return false;
  }

  await replyText(
    ctx,
    "订阅配置损坏，管理功能暂不可用（已记录到日志，请联系维护者）",
  );
  return true;
}

async function rejectNonAdmin(
  ctx: ReplyContext,
  groupId: string,
  userId: string,
): Promise<boolean> {
  if (isAdmin(groupId, userId)) {
    return false;
  }

  await replyText(ctx, "没有权限，该操作仅管理员可用");
  return true;
}

/**
 * ⚠️ `event` / `ctx` 显式标 `any`：`@snowluma/sdk` 未导出这两个事件对象的类型，
 * 这里保持宽松边界（与阶段 0 基线一致），避免 strict 模式下 TS7006 隐式 any 报错。
 */
bot.onGroupMessage(async (event: any, ctx: any) => {
  const groupId = String(event.group_id);
  const userId = String(event.user_id);

  if (!allowedGroups.has(groupId)) {
    return;
  }

  const rawMessage = event.raw_message;
  const match = /^#([^\s]+)(?:\s+([\s\S]*))?$/u.exec(rawMessage);

  if (!match) {
    return;
  }

  const command = match[1];
  const query = match[2];

  if (!command) {
    return;
  }

  try {
    switch (command) {
      case "ping": {
        await replyText(ctx, "pong");
        return;
      }

      case "状态": {
        const status = await bot.getStatus();
        await replyText(ctx, status.online ? "机器人在线" : "机器人离线");
        return;
      }

      case "斗虫汇总": {
        const rooms = await biliGet<RoomSummary[]>("/gift/by_month");
        const liveCount = rooms.filter((room) => room.status === 1).length;
        // 评审 F1：聚合不能把缺项当 0——漏采的房间会把"当月累计流水"整体拉低，
        // 看起来却像一个精确值。这里把「已采集合计」与缺项数一起给出来。
        const known = rooms
          .map((room) => sumRevenueOrNull([room.gift, room.guard, room.super_chat]))
          .filter((value): value is number => value != null);
        const missing = rooms.length - known.length;
        const revenueSum = known.reduce((sum, value) => sum + value, 0);
        const revenueText =
          missing === 0
            ? yuanText(revenueSum)
            : missing === rooms.length
              ? "未采集"
              : `${yuanText(revenueSum)}（另有 ${missing}/${rooms.length} 个房间未采集，未计入）`;

        await replyText(
          ctx,
          [
            `监控房间数：${rooms.length}`,
            `正在直播数：${liveCount}`,
            `当月累计流水：${revenueText}`,
          ].join("\n"),
        );
        return;
      }

      case "营收排行": {
        const rooms = await biliGet<RoomSummary[]>("/gift/by_month");
        // 评审 F1：数据缺失的房间不能按 0 参与排序，那会把它伪装成"流水为 0 的主播"。
        // 缺失的排在末尾，并在文案里明确标出「未采集」。
        const knownRooms = rooms.filter(
          (room) => sumRevenueOrNull([room.gift, room.guard, room.super_chat]) != null,
        );
        const unknownCount = rooms.length - knownRooms.length;
        const ranked = [...knownRooms]
          .sort((a, b) => totalRevenueForSort(b) - totalRevenueForSort(a))
          .slice(0, 10);

        if (ranked.length === 0) {
          // 第四轮评审 P2：不能只说"暂无数据"——那看起来像"没人有流水"，
          // 但真相可能是"有监控房间，只是流水全部未采集"
          await replyText(
            ctx,
            unknownCount > 0
              ? `本月流水暂未采集（${unknownCount} 个监控房间均无可排名数据）`
              : "暂无数据",
          );
          return;
        }

        await replyText(
          ctx,
          [
            // 第四轮评审 P2：标题要写明这是"已采集"范围内的 Top 10，
            // 否则用户会以为它是全部监控房间的排名
            "本月已采集流水 Top 10：",
            ...ranked.map(
              (room, index) =>
                `${index + 1}. ${roomName(room)}：${sumYuanText([room.gift, room.guard, room.super_chat])}`,
            ),
            ...(unknownCount > 0
              ? [`（另有 ${unknownCount} 个房间数据未采集，未参与排名）`]
              : []),
          ].join("\n"),
        );
        return;
      }

      case "直播状态": {
        const rooms = await biliGet<RoomSummary[]>("/gift");
        const liveRooms = rooms.filter((room) => room.status === 1);

        if (liveRooms.length === 0) {
          await replyText(ctx, "当前暂无主播直播");
          return;
        }

        await replyText(
          ctx,
          [
            "当前直播中：",
            ...liveRooms.map(
              (room) =>
                // 两个字段都走各自的唯一出口：
                // ・同接是计数 → countText()（缺失给「未采集」而不是 "-"，评审 F1）
                // ・已播时长是 HH:MM:SS 展示串 → textOrNotCollected()。
                //   ⚠️ 不能走 countText()：Number("12:03:33") 是 NaN，会把有数据的字段
                //   说成「未采集」；也不能直接插值，缺失时会输出 undefined。
                `• ${roomName(room)}（${room.room_id}） 同接：${countText(
                  room.current_concurrency,
                )} 已播：${textOrNotCollected(room.live_duration, 12)}`,
            ),
          ].join("\n"),
        );
        return;
      }

      case "本月统计": {
        const argument = await requireArgument(
          ctx,
          query,
          "请填写主播名或关键词",
        );
        if (typeof argument !== "string") {
          return;
        }

        const roomId = await resolveRoom(ctx, argument);
        if (roomId === null) {
          return;
        }

        const room = await getRoomSummary(roomId, true);
        const sessions = await getSessions(roomId);
        const name = room ? roomName(room) : String(roomId);

        await replyText(ctx, buildMonthlyReport(name, sessions));
        return;
      }

      case "查直播":
      case "本场数据": {
        const argument = await requireArgument(
          ctx,
          query,
          "请填写主播名或关键词",
        );
        if (typeof argument !== "string") {
          return;
        }

        const roomId = await resolveRoom(ctx, argument);
        if (roomId === null) {
          return;
        }

        const [room, sessions, rankings, scRows] = await Promise.all([
          getRoomSummary(roomId),
          getSessions(roomId),
          getRankings(roomId),
          getScRows(roomId),
        ]);

        const name = room ? roomName(room) : String(roomId);
        await replyText(ctx, buildReport(name, sessions, rankings, scRows));
        return;
      }

      case "房间统计": {
        const argument = await requireArgument(
          ctx,
          query,
          "请填写主播名或关键词",
        );
        if (typeof argument !== "string") {
          return;
        }

        const roomId = await resolveRoom(ctx, argument);
        if (roomId === null) {
          return;
        }

        const room = await getRoomSummary(roomId, true);

        if (!room) {
          await replyText(ctx, "暂无数据");
          return;
        }

        await replyText(
          ctx,
          [
            `${roomName(room)}（${room.room_id}）本月统计`,
            `礼物：${yuanText(room.gift)}`,
            // 「上舰」是这一类收入的口径名；不要写成「舰长」——同一条回复里
            // 下面还有「舰团（舰长/提督/总督）」的人数，同名不同量纲会误读（评审 P2）。
            `上舰：${yuanText(room.guard)}`,
            `SC：${yuanText(room.super_chat)}`,
            `总流水：${sumYuanText([room.gift, room.guard, room.super_chat])}`,
            // 评审 F1：这里原来直接插值，缺失时会输出字面量 undefined
            `付费人数：${countText(room.payer_count)}`,
            // 需求 R-04：#房间统计 需含「盲盒」与「舰团」两项，此前漏了
            // 尾项修复：盈亏也不能绕过 numOrNull，否则空串会变成 "0.0 元"
            `盲盒：${countText(room.blind_box_count)} 个｜盈亏：${yuanText(room.blind_box_profit)}`,
            // ⚠️ 不要用 `?? 0`：接口没采集 ≠ 人数为零（评审 N2）
            `舰团（舰长/提督/总督）：${countText(room.guard_1)}/${countText(room.guard_2)}/${countText(room.guard_3)}`,
            `粉丝数：${countText(room.fans_count)}`,
          ].join("\n"),
        );
        return;
      }

      case "场次统计": {
        const argument = await requireArgument(
          ctx,
          query,
          "请填写主播名或关键词",
        );
        if (typeof argument !== "string") {
          return;
        }

        const roomId = await resolveRoom(ctx, argument);
        if (roomId === null) {
          return;
        }

        // 需求 R-04：本命令只回「最近一场详情」，不重复 #查直播 的完整报告。
        const sessions = [...(await getSessions(roomId))].sort((a, b) =>
          a.start_time.localeCompare(b.start_time),
        );
        const latest = sessions.at(-1);

        if (!latest) {
          await replyText(ctx, `房间 ${roomId} 本月还没有已记录场次`);
          return;
        }

        // 评审 F3：原来直接拼斗虫的原始 UTC 串，违反需求 R-10「所有时间均显示北京时间」；
        // 而且漏了 R-04 要求的「时长」。这里统一走 session-report 的三态口径。
        const timing = sessionTiming(latest.start_time, latest.end_time);

        await replyText(
          ctx,
          [
            `房间 ${roomId} 最近场次`,
            `开始：${timing.startText}`,
            `结束：${timing.endText}`,
            `时长：${timing.durationText}`,
            `流水：${sessionTotalText(latest)}`,
            `弹幕：${countText(latest.danmaku_count)}`,
            // 同接是浮点均值，缺失同样给「未采集」，不再给「暂无」（评审 F1）
            `平均同接：${avgText(latest.avg_concurrency)}`,
            `峰值同接：${countText(latest.max_concurrency)}`,
          ].join("\n"),
        );
        return;
      }

      case "SC记录": {
        const argument = await requireArgument(
          ctx,
          query,
          "请填写主播名或关键词",
        );
        if (typeof argument !== "string") {
          return;
        }

        const roomId = await resolveRoom(ctx, argument);
        if (roomId === null) {
          return;
        }

        const rows = await getScRows(roomId);
        const recent = rows.slice(-8).reverse();

        if (recent.length === 0) {
          await replyText(ctx, "暂无 SC 记录");
          return;
        }

        await replyText(
          ctx,
          // 走共享的 scLine()：北京时间、单行、80 字截断、**金额缺失显示「金额未采集」**
          // 全部在里面。第四轮评审的阻断项就是这里原来的 `money(Number(row.price ?? 0))`
          // ——漏采的 price 会被显示成 `0.0 元`，群用户会当成真实金额。
          recent.map(scLine).join("\n"),
        );
        return;
      }

      case "粉丝快照": {
        const argument = await requireArgument(
          ctx,
          query,
          "请填写主播名或关键词",
        );
        if (typeof argument !== "string") {
          return;
        }

        const roomId = await resolveRoom(ctx, argument);
        if (roomId === null) {
          return;
        }

        const result = await biliGet<{ attention: AttentionRow[] }>(
          `/gift/attention?room_id=${encodeURIComponent(String(roomId))}`,
        );
        const latest = result.attention.at(-1);

        if (!latest) {
          await replyText(ctx, "暂无粉丝快照");
          return;
        }

        await replyText(
          ctx,
          [
            // 字段顺序对齐需求 R-04：日期、粉丝数、关注数、舰团三档、当日流水
            // 日期走 dayText()：斗虫给的是 `20261005` 裸串，统一成 `2026-10-05`。
            // ⚠️ 不要用 countText()：它靠 Number() 往返，只在"恰好是纯数字串"时才侥幸
            // 正确，换成任何带分隔符的日期就会退化成「未采集」。
            `日期：${dayText(latest.date)}`,
            `粉丝数：${countText(latest.fans_count)}`,
            // 评审 F1：这里原来直接插值，缺失时会输出字面量 undefined
            `关注数：${countText(latest.attention)}`,
            // ⚠️ 舰团档位对应关系必须与 session-report.ts 一致：
            // guard_1=舰长、guard_2=提督、guard_3=总督。
            // 此前这里写反了（总督=guard_1、舰长=guard_3），会把最贵的档位显示成最便宜的。
            // 也不要用 `?? "-"` 之外的兜底把缺失显示成 0（评审 N2）。
            `舰长：${countText(latest.guard_1)}`,
            `提督：${countText(latest.guard_2)}`,
            `总督：${countText(latest.guard_3)}`,
            // 需求 R-04：#粉丝快照 需含「当日流水」，此前漏了
            // 评审 F1：缺项不能当 0，否则这是"当日流水"的假精确值
            `当日流水：${sumYuanText([latest.gift, latest.guard, latest.super_chat])}`,
          ].join("\n"),
        );
        return;
      }

      case "词云": {
        const argument = await requireArgument(
          ctx,
          query,
          "请填写主播名或关键词",
        );
        if (typeof argument !== "string") {
          return;
        }

        const roomId = await resolveRoom(ctx, argument);
        if (roomId === null) {
          return;
        }

        // 需求 R-04：阶段 3 之前应回「词云功能尚未启用…」。
        // 该接口目前还没在斗虫侧部署，直接请求会 404 并被外层 catch 成"查询暂时不可用"，
        // 对群友是个误导性提示——所以这里单独兜底，把"未启用"和"接口故障"都收敛成明确文案。
        let metrics: DanmakuMetrics;
        try {
          metrics = await biliGet<DanmakuMetrics>(
            `/gift/danmaku_metrics?room_id=${encodeURIComponent(String(roomId))}`,
          );
        } catch {
          await replyText(ctx, "词云功能尚未启用，等待下一场直播后开放");
          return;
        }

        if (!metrics.available) {
          await replyText(ctx, "词云功能尚未启用，等待下一场直播后开放");
          return;
        }

        const words = metrics.words ?? [];
        if (words.length === 0) {
          await replyText(ctx, "暂无短期数据");
          return;
        }

        await replyText(
          ctx,
          [
            "词云：",
            ...words
              .slice(0, 20)
              .map((item, index) => `${index + 1}. ${item.word} ×${countText(item.count)}`),
          ].join("\n"),
        );
        return;
      }

      case "表情排行": {
        const argument = await requireArgument(
          ctx,
          query,
          "请填写主播名或关键词",
        );
        if (typeof argument !== "string") {
          return;
        }

        const roomId = await resolveRoom(ctx, argument);
        if (roomId === null) {
          return;
        }

        // 同上：接口未部署时不报"查询不可用"，而是明确说"尚未启用"（需求 R-04）
        let metrics: DanmakuMetrics;
        try {
          metrics = await biliGet<DanmakuMetrics>(
            `/gift/danmaku_metrics?room_id=${encodeURIComponent(String(roomId))}`,
          );
        } catch {
          await replyText(ctx, "表情排行功能尚未启用，等待下一场直播后开放");
          return;
        }

        if (!metrics.available) {
          await replyText(ctx, "表情排行功能尚未启用，等待下一场直播后开放");
          return;
        }

        const emotes = metrics.emotes ?? [];
        if (emotes.length === 0) {
          await replyText(ctx, "暂无短期数据");
          return;
        }

        await replyText(
          ctx,
          [
            "表情排行：",
            ...emotes
              .slice(0, 20)
              .map((item, index) => `${index + 1}. ${item.name} ×${countText(item.count)}`),
          ].join("\n"),
        );
        return;
      }

      case "订阅": {
        if (await rejectIfStoreDamaged(ctx)) {
          return;
        }

        if (await rejectNonAdmin(ctx, groupId, userId)) {
          return;
        }

        const argument = query?.trim() ?? "";
        if (!argument) {
          await replyText(ctx, "请填写主播名或关键词，例如：#订阅 灰");
          return;
        }

        const target = await resolveAnchorTarget(
          ctx,
          argument,
          "该主播未被监控，无法开启通知",
        );
        if (!target) {
          return;
        }

        if (await rejectIfAnchorUnmonitored(ctx, target.roomId)) {
          return;
        }

        const result = await store.addAnchor(
          groupId,
          userId,
          String(target.roomId),
          target.name,
        );
        await replyText(ctx, result.message);
        return;
      }

      case "退订": {
        if (await rejectIfStoreDamaged(ctx)) {
          return;
        }

        if (await rejectNonAdmin(ctx, groupId, userId)) {
          return;
        }

        const argument = query?.trim() ?? "";
        if (!argument) {
          await replyText(ctx, "请填写主播名或关键词，例如：#退订 灰");
          return;
        }

        const target = await resolveAnchorTarget(ctx, argument);
        if (!target) {
          return;
        }

        const result = await store.removeAnchor(
          groupId,
          userId,
          String(target.roomId),
        );
        await replyText(ctx, result.message);
        return;
      }

      case "订阅列表": {
        if (await rejectIfStoreDamaged(ctx)) {
          return;
        }

        if (await rejectNonAdmin(ctx, groupId, userId)) {
          return;
        }

        const anchors = store.listAnchors(groupId);

        if (anchors.length === 0) {
          await replyText(ctx, "本群暂无主播订阅");
          return;
        }

        await replyText(
          ctx,
          anchors
            .map(
              ({ roomId, name, notify }) =>
                `• ${name}（${roomId}） dynamic=${
                  notify.dynamic ? "on" : "off"
                } live=${notify.live ? "on" : "off"} liveEnd=${
                  notify.liveEnd ? "on" : "off"
                } sc=${notify.sc ? "on" : "off"} atAll=${
                  notify.atAll ? "on" : "off"
                }`,
            )
            .join("\n"),
        );
        return;
      }

      case "通知设置": {
        if (await rejectIfStoreDamaged(ctx)) {
          return;
        }

        if (await rejectNonAdmin(ctx, groupId, userId)) {
          return;
        }

        const input = query?.trim() ?? "";
        if (!input) {
          await replyText(ctx, "请填写主播名或关键词，例如：#通知设置 灰 live=on");
          return;
        }

        const parsed = parseNotifySettings(input);

        if (!parsed.ok) {
          if (parsed.reason === "keyword") {
            await replyText(ctx, "请填写主播名或关键词，例如：#通知设置 灰 live=on");
          } else {
            await replyText(
              ctx,
              "参数格式不正确，例如：#通知设置 灰 live=on",
            );
          }
          return;
        }

        const target = await resolveAnchorTarget(
          ctx,
          parsed.keyword,
          "该主播未被监控，无法开启通知",
        );
        if (!target) {
          return;
        }

        if (await rejectIfAnchorUnmonitored(ctx, target.roomId)) {
          return;
        }

        const result = await store.setNotify(
          groupId,
          userId,
          String(target.roomId),
          parsed.patch,
        );
        await replyText(ctx, result.message);
        return;
      }

      case "特关添加": {
        if (await rejectIfStoreDamaged(ctx)) {
          return;
        }

        if (await rejectNonAdmin(ctx, groupId, userId)) {
          return;
        }

        // 解析抽在 subscriptions.ts 的 parseWatchedUserInput()（决策 D-54：
        // 接线层无单测，可判定的解析逻辑要留在有测试覆盖的一侧），
        // 显示名在其中经 oneLine() 压单行并截断。
        const parsedTarget = parseWatchedUserInput(query ?? "");
        if (!parsedTarget.ok) {
          await replyText(
            ctx,
            "UID 格式不正确，请填写纯数字 UID，例如：#特关添加 8xxxxxxx",
          );
          return;
        }

        const result = await store.addWatchedUser(
          groupId,
          userId,
          parsedTarget.uid,
          parsedTarget.name,
        );
        await replyText(ctx, result.message);
        return;
      }

      case "特关移除": {
        if (await rejectIfStoreDamaged(ctx)) {
          return;
        }

        if (await rejectNonAdmin(ctx, groupId, userId)) {
          return;
        }

        const targetUid = query?.trim() ?? "";
        if (!/^\d+$/u.test(targetUid)) {
          await replyText(ctx, "UID 格式不正确，请填写纯数字 UID，例如：#特关移除 8xxxxxxx");
          return;
        }

        const result = await store.removeWatchedUser(groupId, userId, targetUid);
        await replyText(ctx, result.message);
        return;
      }

      case "特关列表": {
        if (await rejectIfStoreDamaged(ctx)) {
          return;
        }

        if (await rejectNonAdmin(ctx, groupId, userId)) {
          return;
        }

        const watchedUsers = store.listWatchedUsers(groupId);
        if (watchedUsers.length === 0) {
          await replyText(ctx, "本群暂无特关用户");
          return;
        }

        await replyText(
          ctx,
          watchedUsers
            .map(({ uid, name }) => `• ${name}（${uid}）`)
            .join("\n"),
        );
        return;
      }

      case "管理员": {
        if (await rejectIfStoreDamaged(ctx)) {
          return;
        }

        if (await rejectNonAdmin(ctx, groupId, userId)) {
          return;
        }

        // 管理员 = 配置里登记的（按群）+ 环境变量 BOT_ADMIN_IDS 提供的（跨群兜底，
        // 与 SubscriptionStore.isAdmin 的判定口径保持一致）。
        const envAdmins = new Set(envAdminIds);
        const storedAdmins = store.listAdmins(groupId);
        const allAdmins = [...new Set([...storedAdmins, ...envAdminIds])];

        if (allAdmins.length === 0) {
          await replyText(ctx, "本群暂无管理员");
          return;
        }

        await replyText(
          ctx,
          allAdmins
            .map((adminId) =>
              envAdmins.has(adminId)
                ? `${adminId}（环境变量）`
                : adminId,
            )
            .join("\n"),
        );
        return;
      }

      case "帮助": {
        await replyText(ctx, helpText());
        return;
      }

      default:
        return;
    }
  } catch (error: unknown) {
    console.log(
      JSON.stringify({
        type: "bili_query_error",
        command,
        error: errorMessage(error),
      }),
    );

    await replyText(ctx, "bili_douchong 查询暂时不可用，请稍后再试");
  }
});

try {
  await startNotifyApi({ store });
} catch (error: unknown) {
  console.error(
    JSON.stringify({
      type: "notify_api_start_error",
      error: errorMessage(error),
    }),
  );
}

await bot.connect();
console.log("qqbot core connected");
