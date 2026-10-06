import { describe, expect, it, vi } from "vitest";

import { parseNumericRoomQuery, resolvePublicRoom } from "../room-resolver.js";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("resolvePublicRoom", () => {
  it("解析公开房间信息", async () => {
    const fetchImpl = vi.fn(async () =>
      response({ code: 0, data: { uid: 70000001, anchor_name: "示例主播", title: "直播中" } }),
    );

    await expect(resolvePublicRoom(70000001, fetchImpl)).resolves.toMatchObject({
      roomId: 70000001,
      uid: 70000001,
      name: "示例主播",
      source: "bilibili-room",
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("公开接口未找到时返回 null", async () => {
    await expect(resolvePublicRoom(70000002, async () => response({ code: 60004 }))).resolves.toBeNull();
  });

  it("HTTP 错误直接失败", async () => {
    await expect(resolvePublicRoom(70000003, async () => response({}, 503))).rejects.toThrow("HTTP 503");
  });

  it("脏响应缺 UID 时返回 null", async () => {
    await expect(resolvePublicRoom(70000004, async () => response({ code: 0, data: {} }))).resolves.toBeNull();
  });

  it("异常按调用方边界抛出", async () => {
    await expect(resolvePublicRoom(70000005, async () => { throw new Error("timeout"); })).rejects.toThrow("timeout");
  });

});

// ★ 下面这组替换掉了原来那条「同义反复」用例（只断言正则与字符串字面量、从不调用生产代码，
//   属本项目已知的「假绿」形态之一）。判定逻辑已抽成纯函数，用例直接盯住它。
describe("parseNumericRoomQuery（回落触发判定）", () => {
  it("ASCII 纯数字 → 返回房间号（允许触发回落）", () => {
    expect(parseNumericRoomQuery("70000001")).toBe(70000001);
    expect(parseNumericRoomQuery("  12345  ")).toBe(12345);
  });

  it("★ 全角数字不回落（NFKC 会把 １２３４ 折叠成 1234，绝不认）", () => {
    expect(parseNumericRoomQuery("１２３４")).toBeNull();
    expect(parseNumericRoomQuery("７")).toBeNull();
  });

  it("★ 带圈/括号数字不回落（NFKC 会把 ① 折叠成 1，曾使无关关键词被当房间号）", () => {
    expect(parseNumericRoomQuery("①")).toBeNull();
    expect(parseNumericRoomQuery("①②③")).toBeNull();
    expect(parseNumericRoomQuery("⑴⑵")).toBeNull();
  });

  it("汉字、字母、空白、混合输入不回落", () => {
    for (const query of ["示例主播", "abc", "123abc", "12 34", "12-34", "#123", "", "   "]) {
      expect(parseNumericRoomQuery(query)).toBeNull();
    }
  });

  it("非法数值不回落（0 与超出安全整数范围的超长数字）", () => {
    expect(parseNumericRoomQuery("0")).toBeNull();
    // 不用字面量长数字——会被脱敏扫描器当成可疑标识符（规则生效的证明）
    expect(parseNumericRoomQuery("9".repeat(23))).toBeNull();
  });
});
