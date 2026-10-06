import { mkdtemp, open as realOpen, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

/**
 * 这组用例回答 2026-10-05 代码评审提出的一个尖锐问题：
 *
 *   「你的并发测试**不能证明**队列串行——去掉队列后，该测试是否必然失败？答案是否定的。
 *     四个 addAnchor 在第一次真正 I/O await 之前都会同步改共享内存，
 *     某些无队列实现里四次写盘仍可能都看到已包含四个主播的对象，最终内存与磁盘都满足断言。
 *     这是假阳性。真正能证明队列的用例应记录**同时在途的 persist 数**，断言其最大值始终为 1。」
 *
 * 于是这里把 `node:fs/promises` 的 `open` 换成可观测版本：
 * 每次原子写都要 `open(tmp, 'w')`，所以「同时在途的 open 数」就是「同时在途的写盘数」。
 * 再人为把写入窗口拉长到 25ms，让任何并发重叠都无所遁形。
 *
 * 判据：`maxInflight === 1`。
 * ——若把 `SubscriptionStore` 里的 `enqueueMutation`（promise 串行队列）去掉，
 *    四个并发 mutation 会各自进入写盘，`maxInflight` 会变成 4，本用例必然失败。
 */
const probe = {
  inflight: 0,
  maxInflight: 0,
  opens: 0,
  failNextOpen: false,
  /** 临时文件已 open 成功、但写入阶段失败（评审 F4 要求的更细粒度故障注入） */
  failNextWriteFile: false,
  /** 临时文件已写完、但 fsync 阶段失败 */
  failNextSync: false,
};

function resetProbe(): void {
  probe.inflight = 0;
  probe.maxInflight = 0;
  probe.opens = 0;
  probe.failNextOpen = false;
  probe.failNextWriteFile = false;
  probe.failNextSync = false;
}

beforeEach(resetProbe);
afterEach(resetProbe);

import { SubscriptionStore } from '../subscriptions.js';

const GROUP = '900000001';
const ADMIN = '800000001';

function createStore(filePath: string): SubscriptionStore {
  return new SubscriptionStore({
    filePath,
    bootstrapAdmins: [ADMIN],
    fileOperations: {
      open: async (...args) => {
        if (probe.failNextOpen) {
          probe.failNextOpen = false;
          throw new Error('模拟写盘失败（测试注入）');
        }

        probe.opens += 1;
        probe.inflight += 1;
        probe.maxInflight = Math.max(probe.maxInflight, probe.inflight);
        // 放大写入窗口：不这样做的话，即使实现有并发问题也可能恰好串不到一起
        await new Promise((resolve) => setTimeout(resolve, 25));

        let real: Awaited<ReturnType<typeof realOpen>>;
        try {
          real = await realOpen(...args);
        } catch (error) {
          probe.inflight -= 1;
          throw error;
        }

        let released = false;
        const release = () => {
          if (!released) {
            released = true;
            probe.inflight -= 1;
          }
        };

        return {
          writeFile: (data: string, encoding: BufferEncoding) => {
            if (probe.failNextWriteFile) {
              probe.failNextWriteFile = false;
              return Promise.reject(new Error('模拟 writeFile 失败（测试注入）'));
            }
            return real.writeFile(data, encoding);
          },
          sync: () => {
            if (probe.failNextSync) {
              probe.failNextSync = false;
              return Promise.reject(new Error('模拟 fsync 失败（测试注入）'));
            }
            return real.sync();
          },
          close: async () => {
            try {
              return await real.close();
            } finally {
              release();
            }
          },
        } as unknown as Awaited<ReturnType<typeof realOpen>>;
      },
    },
  });
}

async function withTempDir<T>(run: (dir: string) => Promise<T>): Promise<T> {
  const dir = await mkdtemp(join(tmpdir(), 'qqbot-core-serial-'));
  try {
    return await run(dir);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

describe('写入串行性（可证伪）', () => {
  it('4 个并发 addAnchor：同时在途的写盘数最大值为 1', async () => {
    await withTempDir(async (dir) => {
      probe.inflight = 0;
      probe.maxInflight = 0;
      probe.opens = 0;

      const store = createStore(join(dir, 'subscriptions.json'));
      await store.load();

      const results = await Promise.all([
        store.addAnchor(GROUP, ADMIN, '700000011', '主播甲'),
        store.addAnchor(GROUP, ADMIN, '700000012', '主播乙'),
        store.addAnchor(GROUP, ADMIN, '700000013', '主播丙'),
        store.addAnchor(GROUP, ADMIN, '700000014', '主播丁'),
      ]);

      expect(results.every((r) => r.ok)).toBe(true);
      // 四次 mutation → 恰好四次原子写
      expect(probe.opens).toBe(4);
      // ★ 核心判据：任何时刻都不存在两个并行的写盘
      expect(probe.maxInflight).toBe(1);
    });
  });

  it('第一笔写失败并回滚，不会抹掉同时发起的第二笔成功写入', async () => {
    await withTempDir(async (dir) => {
      const filePath = join(dir, 'subscriptions.json');
      const store = createStore(filePath);
      await store.load();

      // 让下一次 open 直接失败 → 第一笔 mutation 的原子写失败并回滚
      probe.failNextOpen = true;

      const [first, second] = await Promise.all([
        store.addAnchor(GROUP, ADMIN, '700000021', '会失败的主播'),
        store.addAnchor(GROUP, ADMIN, '700000022', '会成功的主播'),
      ]);

      // 队列保持入队顺序：第一笔失败，第二笔成功
      expect(first.ok).toBe(false);
      expect(second.ok).toBe(true);

      // ★ 关键：第二笔的成功结果既在内存里，也在磁盘上，没有被第一笔的回滚抹掉
      expect(store.listAnchors(GROUP).map((a) => a.roomId)).toEqual(['700000022']);

      const onDisk = JSON.parse(await readFile(filePath, 'utf8')) as {
        groups: Record<string, { anchors: Record<string, unknown> }>;
      };
      expect(Object.keys(onDisk.groups[GROUP]?.anchors ?? {})).toEqual(['700000022']);
    });
  });
});

/**
 * 原子写的**故障注入**（第三轮评审 F4）。
 *
 * 上一轮只覆盖了「临时文件 open 就失败」这一种最粗的情况。
 * 但真正危险的是**临时文件已经 open 成功、内容写到一半或 fsync 时才失败**：
 * 这时临时文件里已经有半截 JSON，如果实现顺序写错（比如直接往目标文件写、
 * 或失败后不清理临时文件），原配置文件就会被写坏或留下垃圾。
 *
 * 判据三条，缺一不可：
 *   ① 返回失败且不抛异常；② 磁盘上的原文件**逐字节不变**；③ 内存回滚；
 *   ④ `${filePath}.tmp` 被清理掉（不留半截文件）。
 */
describe('原子写故障注入：open 成功但 writeFile / fsync 失败', () => {
  for (const stage of ['writeFile', 'sync'] as const) {
    it(`${stage} 阶段失败：原文件逐字节不变、内存回滚、临时文件被清理`, async () => {
      await withTempDir(async (dir) => {
        const filePath = join(dir, 'subscriptions.json');
        const store = createStore(filePath);
        await store.load();

        // 先落一份**有效**配置，作为"原文件"的基线
        expect((await store.addAnchor(GROUP, ADMIN, '700000031', '原有主播')).ok).toBe(true);
        const originalBytes = await readFile(filePath);
        const originalSnapshot = store.snapshot();

        // 注入：临时文件能成功 open，但写入 / fsync 阶段炸掉
        if (stage === 'writeFile') probe.failNextWriteFile = true;
        else probe.failNextSync = true;

        const failed = await store.addAnchor(GROUP, ADMIN, '700000032', '写不进去的主播');

        expect(failed.ok).toBe(false);
        expect(failed.message).toBe('配置写入失败，请稍后再试');

        // ② 原文件逐字节不变——半截写入绝不能碰到它
        expect((await readFile(filePath)).equals(originalBytes)).toBe(true);
        // ③ 内存回滚
        expect(store.snapshot()).toEqual(originalSnapshot);
        expect(store.listAnchors(GROUP).map((a) => a.roomId)).toEqual(['700000031']);
        // ④ 临时文件被清理，不留半截 JSON
        await expect(readFile(`${filePath}.tmp`)).rejects.toThrow();

        // 故障注入是一次性的：紧接着的同类写入必须能成功（证明不是被永久卡住）
        probe.failNextWriteFile = false;
        probe.failNextSync = false;
        const recovered = await store.addAnchor(GROUP, ADMIN, '700000033', '恢复正常的主播');
        expect(recovered.ok).toBe(true);
        expect(store.listAnchors(GROUP).map((a) => a.roomId)).toEqual([
          '700000031',
          '700000033',
        ]);
      });
    });
  }
});
