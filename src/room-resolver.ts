export type ResolvedAnchor = {
  roomId: number;
  uid?: number;
  name: string;
  title?: string;
  cover?: string;
  area?: string;
  source: "bilibili-room";
};

type PublicRoomPayload = {
  code?: unknown;
  data?: {
    uid?: unknown;
    anchor_name?: unknown;
    title?: unknown;
    user_cover?: unknown;
    cover?: unknown;
    parent_area_name?: unknown;
    area_name?: unknown;
  };
};

export type FetchLike = (
  input: string,
  init?: RequestInit,
) => Promise<Response>;

function positiveId(value: unknown): number | undefined {
  const id = typeof value === "number" ? value : Number(value);
  return Number.isInteger(id) && id > 0 ? id : undefined;
}

function textOrUndefined(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : undefined;
}

/**
 * 判断用户输入是否应触发公开房间回落查询（R-01 / T-505 冻结行为 3）。
 *
 * ★ 只认 **ASCII 纯数字**，**不做 NFKC 归一化**：
 *   NFKC 会把全角数字（`１２３` → `123`）甚至带圈数字（`①` → `1`）折叠成数字，
 *   于是「①②③」这种无关关键词会被当成房间号送进公开接口——若恰好命中一个真实房间，
 *   机器人会报出一个**看起来正确、实则错误的房间**（静默错误数据，本项目最忌讳的一类）。
 *
 * 抽成纯函数而不是写在 `index.ts` 里，是因为 `index.ts` 无单测、唯一安全网是实发（D-54/D-79）；
 * 判定逻辑落在这里才能被用例直接盯住。
 *
 * @param raw 用户输入的关键词（未处理）
 * @returns 合法时返回房间号；否则 `null`（调用方保留「没有匹配到已监控主播」文案）
 */
export function parseNumericRoomQuery(raw: string): number | null {
  const trimmed = raw.trim();
  if (!/^[0-9]+$/.test(trimmed)) {
    return null;
  }

  const value = Number(trimmed);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

export async function resolvePublicRoom(
  roomId: number,
  fetchImpl: FetchLike = fetch,
): Promise<ResolvedAnchor | null> {
  const response = await fetchImpl(
    `https://api.live.bilibili.com/room/v1/Room/get_info?room_id=${roomId}`,
    { signal: AbortSignal.timeout(8_000) },
  );

  if (!response.ok) {
    throw new Error(`房间接口 HTTP ${response.status}`);
  }

  const payload = (await response.json()) as PublicRoomPayload;
  if (payload.code !== 0 || payload.data === undefined) {
    return null;
  }

  const uid = positiveId(payload.data.uid);
  if (uid === undefined) {
    return null;
  }

  const name =
    textOrUndefined(payload.data.anchor_name) ??
    textOrUndefined(payload.data.title) ??
    String(uid);

  return {
    roomId,
    uid,
    name,
    title: textOrUndefined(payload.data.title),
    cover:
      textOrUndefined(payload.data.user_cover) ??
      textOrUndefined(payload.data.cover),
    area: [
      textOrUndefined(payload.data.parent_area_name),
      textOrUndefined(payload.data.area_name),
    ]
      .filter((value): value is string => value !== undefined)
      .join(" / ") || undefined,
    source: "bilibili-room",
  };
}
