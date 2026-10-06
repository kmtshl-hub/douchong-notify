import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import {
  parseWatchedUserInput,
  SubscriptionsFileError,
  SubscriptionStore,
  WATCHED_NAME_MAX_LENGTH,
} from '../subscriptions.js';
import {
  createDeduper,
  createMemoryDedupStore,
} from '../dedup.js';
import { selectRooms } from '../room-selector.js';

type TestRoom = {
  room_id: number;
  anchor_name: string;
};

// vitest.config.ts 将 TEMP 指向工作区以隔离转换缓存；业务原子写测试需避开
// 同步盘目录，否则 Windows 的 rename(tmp, target) 会被同步客户端短暂占用。
const testTempRoot = join(
  process.env.LOCALAPPDATA ?? tmpdir(),
  'qqbot-vitest-tests',
);

async function withTempDir<T>(
  run: (directory: string) => Promise<T>,
): Promise<T> {
  await mkdir(testTempRoot, { recursive: true });
  const directory = await mkdtemp(join(testTempRoot, 'qqbot-core-'));

  try {
    return await run(directory);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

describe('阶段 1 核心模块', () => {
  it('唯一主播名匹配', () => {
    const rooms: TestRoom[] = [
      { room_id: 1001, anchor_name: '星河' },
      { room_id: 1002, anchor_name: '月光' },
    ];

    expect(selectRooms(rooms, '星河')).toEqual([
      { room_id: 1001, anchor_name: '星河' },
    ]);
  });

  it('多主播名匹配时返回多个候选，不自动选择', () => {
    const rooms: TestRoom[] = [
      { room_id: 2001, anchor_name: '小熊一号' },
      { room_id: 2002, anchor_name: '小熊二号' },
      { room_id: 2003, anchor_name: '白兔' },
    ];

    expect(selectRooms(rooms, '小熊')).toEqual([
      { room_id: 2001, anchor_name: '小熊一号' },
      { room_id: 2002, anchor_name: '小熊二号' },
    ]);
  });

  it('查询未知主播返回空数组', () => {
    const rooms: TestRoom[] = [
      { room_id: 3001, anchor_name: '主播甲' },
      { room_id: 3002, anchor_name: '主播乙' },
    ];

    expect(selectRooms(rooms, '不存在的主播')).toEqual([]);
  });

  it('房间号直接匹配且纯数字不走名称路径', () => {
    const rooms: TestRoom[] = [
      { room_id: 123, anchor_name: '真正的房间 123' },
      { room_id: 456, anchor_name: '123号机' },
    ];

    expect(selectRooms(rooms, '123')).toEqual([
      { room_id: 123, anchor_name: '真正的房间 123' },
    ]);
  });

  it('群 A 添加主播后不会出现在群 B', async () => {
    await withTempDir(async (directory) => {
      const filePath = join(directory, 'subscriptions.json');
      const store = new SubscriptionStore({
        filePath,
        bootstrapAdmins: ['800000001'],
      });

      await store.load();

      const result = await store.addAnchor(
        '900000001',
        '800000001',
        '700000001',
        '示例主播',
      );

      expect(result.ok).toBe(true);
      expect(store.listAnchors('900000001')).toHaveLength(1);
      expect(store.listAnchors('900000002')).toEqual([]);
    });
  });

  it('普通群员执行 addAnchor 被拒绝且文件内容不变', async () => {
    await withTempDir(async (directory) => {
      const filePath = join(directory, 'subscriptions.json');
      const initialContent = `${JSON.stringify(
        {
          version: 1,
          groups: {
            '900000001': {
              admins: ['800000001'],
              anchors: {},
            },
          },
        },
        null,
        2,
      )}\n`;

      await writeFile(filePath, initialContent, 'utf8');

      const store = new SubscriptionStore({ filePath });
      await store.load();

      const before = await readFile(filePath);
      const result = await store.addAnchor(
        '900000001',
        '800000002',
        '700000001',
        '示例主播',
      );
      const after = await readFile(filePath);

      expect(result.ok).toBe(false);
      expect(after.equals(before)).toBe(true);
      expect(store.listAnchors('900000001')).toEqual([]);
    });
  });

  it('管理员执行 addAnchor 后 listAnchors 含该主播', async () => {
    await withTempDir(async (directory) => {
      const filePath = join(directory, 'subscriptions.json');
      const store = new SubscriptionStore({
        filePath,
        bootstrapAdmins: ['800000001'],
      });

      await store.load();

      const result = await store.addAnchor(
        '900000001',
        '800000001',
        '700000001',
        '示例主播',
      );

      expect(result.ok).toBe(true);
      expect(store.listAnchors('900000001')).toEqual([
        {
          roomId: '700000001',
          name: '示例主播',
          notify: {
            dynamic: true,
            live: true,
            liveEnd: true,
            sc: false,
            atAll: false,
          },
        },
      ]);
    });
  });

  it('写入后新建 store load 可恢复相同内容和 notify 开关', async () => {
    await withTempDir(async (directory) => {
      const filePath = join(directory, 'subscriptions.json');
      const firstStore = new SubscriptionStore({
        filePath,
        bootstrapAdmins: ['800000001'],
      });

      await firstStore.load();
      expect(
        (
          await firstStore.addAnchor(
            '900000001',
            '800000001',
            '700000001',
            '示例主播',
          )
        ).ok,
      ).toBe(true);

      expect(
        (
          await firstStore.setNotify(
            '900000001',
            '800000001',
            '700000001',
            {
              dynamic: false,
              liveEnd: false,
              atAll: true,
            },
          )
        ).ok,
      ).toBe(true);

      const beforeReload = firstStore.snapshot();

      const secondStore = new SubscriptionStore({ filePath });
      await secondStore.load();

      expect(secondStore.snapshot()).toEqual(beforeReload);
      expect(secondStore.listAnchors('900000001')[0]?.notify).toEqual({
        dynamic: false,
        live: true,
        liveEnd: false,
        sc: false,
        atAll: true,
      });
    });
  });

  it('相同 event_id 会去重且 TTL 过期后可再次登记', async () => {
    let now = 1_000_000;
    const memoryStore = createMemoryDedupStore({
      now: () => now,
    });
    const deduper = createDeduper(memoryStore, 10);

    expect(
      await deduper.isDuplicate('live', 700000001, 'evt-1'),
    ).toBe(false);
    expect(
      await deduper.isDuplicate('live', 700000001, 'evt-1'),
    ).toBe(true);

    now += 10_001;

    expect(
      await deduper.isDuplicate('live', 700000001, 'evt-1'),
    ).toBe(false);
  });

  it('非法 JSON load 抛错、不改文件且损坏后停止写入', async () => {
    await withTempDir(async (directory) => {
      const filePath = join(directory, 'subscriptions.json');
      const invalidContent = Buffer.from(
        '{"version":1,"groups":{BROKEN',
        'utf8',
      );

      await writeFile(filePath, invalidContent);

      const store = new SubscriptionStore({
        filePath,
        bootstrapAdmins: ['800000001'],
      });

      let caught: unknown;
      try {
        await store.load();
      } catch (error: unknown) {
        caught = error;
      }

      expect(caught).toBeInstanceOf(SubscriptionsFileError);
      expect(store.loaded).toBe(false);

      const afterLoad = await readFile(filePath);
      expect(afterLoad.equals(invalidContent)).toBe(true);

      const result = await store.addAnchor(
        '900000001',
        '800000001',
        '700000001',
        '示例主播',
      );

      expect(result).toEqual({
        ok: false,
        message: '订阅配置损坏，已停止写入以避免覆盖',
      });

      const afterWriteAttempt = await readFile(filePath);
      expect(afterWriteAttempt.equals(invalidContent)).toBe(true);
    });
  });
});

/**
 * 以下用例是 2026-10-05 代码评审（Claude Opus 5.5）指出的覆盖缺口补的。
 * 上面 10 个是阶段 1 任务书要求的必需用例，保持原样不动；这一组是新增。
 */
describe('阶段 1 评审整改补充', () => {
  const GROUP = '900000001';
  const ADMIN = '800000001';
  const STRANGER = '800000002';

  async function freshStore(directory: string) {
    const filePath = join(directory, 'subscriptions.json');
    const store = new SubscriptionStore({ filePath, bootstrapAdmins: [ADMIN] });
    await store.load();
    return { filePath, store };
  }

  it('removeAnchor 成功路径：内存与磁盘都移除', async () => {
    await withTempDir(async (directory) => {
      const { filePath, store } = await freshStore(directory);

      expect((await store.addAnchor(GROUP, ADMIN, '700000001', '示例主播')).ok).toBe(true);
      const removed = await store.removeAnchor(GROUP, ADMIN, '700000001');

      expect(removed.ok).toBe(true);
      expect(store.listAnchors(GROUP)).toEqual([]);

      const onDisk = JSON.parse(await readFile(filePath, 'utf8')) as {
        groups: Record<string, { anchors: Record<string, unknown> }>;
      };
      expect(Object.keys(onDisk.groups[GROUP]?.anchors ?? {})).toEqual([]);
    });
  });

  it('removeAnchor 未订阅的主播：返回明确拒绝且不动文件', async () => {
    await withTempDir(async (directory) => {
      const { filePath, store } = await freshStore(directory);
      await store.addAnchor(GROUP, ADMIN, '700000001', '示例主播');

      const before = await readFile(filePath);
      const removed = await store.removeAnchor(GROUP, ADMIN, '700000099');
      const after = await readFile(filePath);

      expect(removed.ok).toBe(false);
      expect(removed.message).toBe('该主播不在本群订阅列表中');
      expect(after.equals(before)).toBe(true);
    });
  });

  it('setNotify 非管理员被拒绝且不改配置', async () => {
    await withTempDir(async (directory) => {
      const { filePath, store } = await freshStore(directory);
      await store.addAnchor(GROUP, ADMIN, '700000001', '示例主播');

      const before = await readFile(filePath);
      const result = await store.setNotify(GROUP, STRANGER, '700000001', { live: false });
      const after = await readFile(filePath);

      expect(result.ok).toBe(false);
      expect(result.message).toBe('没有权限，该操作仅管理员可用');
      expect(after.equals(before)).toBe(true);
      expect(store.listAnchors(GROUP)[0]?.notify.live).toBe(true);
    });
  });

  it('addAdmin：管理员可添加、非管理员被拒、重复添加幂等', async () => {
    await withTempDir(async (directory) => {
      const { store } = await freshStore(directory);

      const denied = await store.addAdmin(GROUP, STRANGER, '800000009');
      expect(denied.ok).toBe(false);
      expect(store.isAdmin(GROUP, '800000009')).toBe(false);

      const added = await store.addAdmin(GROUP, ADMIN, '800000009');
      expect(added.ok).toBe(true);
      expect(store.isAdmin(GROUP, '800000009')).toBe(true);

      const again = await store.addAdmin(GROUP, ADMIN, '800000009');
      expect(again.ok).toBe(true);
      expect(store.listAdmins(GROUP).filter((id) => id === '800000009')).toHaveLength(1);
    });
  });

  it('并发写：多个 addAnchor 同时发起不丢更新且落盘一致', async () => {
    await withTempDir(async (directory) => {
      const { filePath, store } = await freshStore(directory);

      const results = await Promise.all([
        store.addAnchor(GROUP, ADMIN, '700000011', '主播甲'),
        store.addAnchor(GROUP, ADMIN, '700000012', '主播乙'),
        store.addAnchor(GROUP, ADMIN, '700000013', '主播丙'),
        store.addAnchor(GROUP, ADMIN, '700000014', '主播丁'),
      ]);

      expect(results.every((r) => r.ok)).toBe(true);
      expect(store.listAnchors(GROUP).map((a) => a.roomId)).toEqual([
        '700000011',
        '700000012',
        '700000013',
        '700000014',
      ]);

      const onDisk = JSON.parse(await readFile(filePath, 'utf8')) as {
        groups: Record<string, { anchors: Record<string, unknown> }>;
      };
      expect(Object.keys(onDisk.groups[GROUP]?.anchors ?? {}).sort()).toEqual([
        '700000011',
        '700000012',
        '700000013',
        '700000014',
      ]);

      // 并发结束后不应残留临时文件
      await expect(readFile(`${filePath}.tmp`)).rejects.toThrow();
    });
  });

  /**
   * 写盘失败必须验到三件事：**返回失败**、**内存回滚**、**磁盘不受损**。
   *
   * ⚠️ 故障注入方式必须**跨平台一致**（2026-10-05 在测试机实测到的问题）：
   * 早期写法是"把配置文件的父路径做成一个普通文件"，靠 `mkdir` 失败来触发回滚。
   * 但 `load()` 阶段读取该路径时，**Windows 报 `ENOENT`（被当成全新配置）、
   * Linux 报 `ENOTDIR`（抛错）** —— 于是同一条用例在 Windows 上"意外通过"、
   * 在真实 Linux 部署环境直接失败。这是典型的平台相关假阳性。
   *
   * 现在改用「把临时文件路径占成目录」：原子写的第一步固定是 `open(<file>.tmp, 'w')`，
   * 这在两个平台上都必然失败，且不影响 `load()`。
   */
  it('写盘失败：返回失败、内存回滚到写入前、磁盘配置不受损', async () => {
    await withTempDir(async (directory) => {
      const { filePath, store } = await freshStore(directory);
      // 先建立一份**有效配置**：这样"回滚"才有可观察的对象
      // （早期版本用一个全新的空 store 断言 listAnchors 为 []，那是同义反复，测不出回滚）
      expect((await store.addAnchor(GROUP, ADMIN, '700000001', '既有主播')).ok).toBe(true);
      const before = store.snapshot();
      const beforeBytes = await readFile(filePath);

      // 跨平台一致的故障注入：临时文件路径被目录占住 → open(<file>.tmp,'w') 必然失败
      await mkdir(`${filePath}.tmp`, { recursive: true });

      const failed = await store.addAnchor(GROUP, ADMIN, '700000002', '写不进去的主播');

      expect(failed.ok).toBe(false);
      expect(failed.message).toBe('配置写入失败，请稍后再试');
      // 内存回滚：只剩写入前那条
      expect(store.listAnchors(GROUP).map((a) => a.roomId)).toEqual(['700000001']);
      expect(store.snapshot()).toEqual(before);
      // 磁盘不受损
      expect((await readFile(filePath)).equals(beforeBytes)).toBe(true);
    });
  });

  it('原子性：已有有效配置文件时写入失败，磁盘上的原文件必须一字不改', async () => {
    await withTempDir(async (directory) => {
      const { filePath, store } = await freshStore(directory);
      expect((await store.addAnchor(GROUP, ADMIN, '700000001', '原有主播')).ok).toBe(true);

      const originalBytes = await readFile(filePath);
      const originalSnapshot = store.snapshot();

      // 用「目录占位」让临时文件创建失败：原子写的第一步是 open(`${filePath}.tmp`)
      await mkdir(`${filePath}.tmp`, { recursive: true });

      const failed = await store.addAnchor(GROUP, ADMIN, '700000099', '写不进去的主播');

      expect(failed.ok).toBe(false);
      expect(failed.message).toBe('配置写入失败，请稍后再试');
      // 内存回滚：新增的那个主播不能留在内存里
      expect(store.listAnchors(GROUP).map((a) => a.roomId)).toEqual(['700000001']);
      expect(store.snapshot()).toEqual(originalSnapshot);
      // ★ 关键：磁盘上的原文件逐字节未变（rename 没发生，原文件没被截断或写坏）
      expect((await readFile(filePath)).equals(originalBytes)).toBe(true);
    });
  });

  /** 配置损坏时，读接口也不能"看起来正常"——上层要能靠 loaded 判断（评审 P1-5 的用户侧） */
  it('配置损坏后 loaded 为 false，写操作一律拒绝且不触碰原文件', async () => {
    await withTempDir(async (directory) => {
      const filePath = join(directory, 'subscriptions.json');
      const broken = Buffer.from('{"version":1,"groups":{BROKEN', 'utf8');
      await writeFile(filePath, broken);

      const store = new SubscriptionStore({ filePath, bootstrapAdmins: [ADMIN] });
      await expect(store.load()).rejects.toThrow(SubscriptionsFileError);
      expect(store.loaded).toBe(false);

      for (const result of [
        await store.addAnchor(GROUP, ADMIN, '700000031', '甲'),
        await store.removeAnchor(GROUP, ADMIN, '700000031'),
        await store.setNotify(GROUP, ADMIN, '700000031', { live: false }),
        await store.addAdmin(GROUP, ADMIN, '800000031'),
      ]) {
        expect(result.ok).toBe(false);
        expect(result.message).toBe('订阅配置损坏，已停止写入以避免覆盖');
      }

      expect((await readFile(filePath)).equals(broken)).toBe(true);
    });
  });
});

describe('T-402 Batch 1：订阅 schema v2 与特关名单', () => {
  const GROUP_A = '900000001';
  const GROUP_B = '900000002';
  const ADMIN = '800000001';
  const STRANGER = '800000002';

  async function freshStore(directory: string) {
    const filePath = join(directory, 'subscriptions.json');
    const store = new SubscriptionStore({
      filePath,
      bootstrapAdmins: [ADMIN],
    });
    await store.load();
    return { filePath, store };
  }

  it('v1 文件读入后补齐 sc / guestEntryEnabled / watchedUsers，快照版本升级为 2', async () => {
    await withTempDir(async (directory) => {
      const filePath = join(directory, 'subscriptions.json');
      await writeFile(
        filePath,
        JSON.stringify({
          version: 1,
          groups: {
            [GROUP_A]: {
              admins: [ADMIN],
              anchors: {
                '700000001': {
                  name: '旧版主播',
                  notify: {
                    dynamic: false,
                    live: true,
                    liveEnd: false,
                    atAll: true,
                  },
                },
              },
            },
          },
        }),
        'utf8',
      );

      const store = new SubscriptionStore({ filePath });
      await store.load();

      const snapshot = store.snapshot();
      expect(snapshot.version).toBe(2);
      expect(snapshot.groups[GROUP_A]?.guestEntryEnabled).toBe(false);
      expect(snapshot.groups[GROUP_A]?.watchedUsers).toEqual([]);
      expect(snapshot.groups[GROUP_A]?.anchors['700000001']?.notify).toEqual({
        dynamic: false,
        live: true,
        liveEnd: false,
        sc: false,
        atAll: true,
      });

      // snapshot 必须是深拷贝：改快照不能改 store 内部状态。
      snapshot.groups[GROUP_A]?.watchedUsers.push({
        uid: '800000099',
        name: '只改快照',
      });
      expect(store.snapshot().groups[GROUP_A]?.watchedUsers).toEqual([]);
    });
  });

  it('addWatchedUser 成功后内存与磁盘都有，重新 load 后仍在', async () => {
    await withTempDir(async (directory) => {
      const { filePath, store } = await freshStore(directory);

      const result = await store.addWatchedUser(
        GROUP_A,
        ADMIN,
        '800000011',
        '特关甲',
      );
      expect(result).toEqual({
        ok: true,
        message: '已添加特关用户：特关甲（800000011）',
      });
      expect(store.listWatchedUsers(GROUP_A)).toEqual([
        { uid: '800000011', name: '特关甲' },
      ]);

      const onDisk = JSON.parse(await readFile(filePath, 'utf8')) as {
        version: number;
        groups: Record<string, { watchedUsers: Array<{ uid: string; name: string }> }>;
      };
      expect(onDisk.version).toBe(2);
      expect(onDisk.groups[GROUP_A]?.watchedUsers).toEqual([
        { uid: '800000011', name: '特关甲' },
      ]);

      const reloaded = new SubscriptionStore({ filePath });
      await reloaded.load();
      expect(reloaded.listWatchedUsers(GROUP_A)).toEqual([
        { uid: '800000011', name: '特关甲' },
      ]);
    });
  });

  it('addWatchedUser 重复 UID 幂等，名单不重复且保留原名称', async () => {
    await withTempDir(async (directory) => {
      const { store } = await freshStore(directory);
      expect(
        (await store.addWatchedUser(GROUP_A, ADMIN, '800000012', '原名称')).ok,
      ).toBe(true);

      const again = await store.addWatchedUser(
        GROUP_A,
        ADMIN,
        '800000012',
        '新名称',
      );

      expect(again.ok).toBe(true);
      expect(again.message).toContain('已在特关列表中');
      expect(store.listWatchedUsers(GROUP_A)).toEqual([
        { uid: '800000012', name: '原名称' },
      ]);
    });
  });

  it('removeWatchedUser 成功移除；不存在 UID 返回明确失败且不改文件', async () => {
    await withTempDir(async (directory) => {
      const { filePath, store } = await freshStore(directory);
      await store.addWatchedUser(GROUP_A, ADMIN, '800000013', '待移除');

      const removed = await store.removeWatchedUser(
        GROUP_A,
        ADMIN,
        '800000013',
      );
      expect(removed).toEqual({
        ok: true,
        message: '已移除特关用户：800000013',
      });
      expect(store.listWatchedUsers(GROUP_A)).toEqual([]);

      const before = await readFile(filePath);
      const missing = await store.removeWatchedUser(
        GROUP_A,
        ADMIN,
        '800000099',
      );
      const after = await readFile(filePath);

      expect(missing.ok).toBe(false);
      expect(missing.message).toBe('该特关用户不在本群列表中：800000099');
      expect(after.equals(before)).toBe(true);
    });
  });

  it('listWatchedUsers 按群隔离，群 A 名单不会出现在群 B', async () => {
    await withTempDir(async (directory) => {
      const { store } = await freshStore(directory);
      await store.addWatchedUser(GROUP_A, ADMIN, '800000021', '群A用户');
      await store.addWatchedUser(GROUP_B, ADMIN, '800000022', '群B用户');

      expect(store.listWatchedUsers(GROUP_A)).toEqual([
        { uid: '800000021', name: '群A用户' },
      ]);
      expect(store.listWatchedUsers(GROUP_B)).toEqual([
        { uid: '800000022', name: '群B用户' },
      ]);
    });
  });

  it('非管理员不能增删特关名单，读取不产生写入且文件内容不变', async () => {
    await withTempDir(async (directory) => {
      const { filePath, store } = await freshStore(directory);
      await store.addWatchedUser(GROUP_A, ADMIN, '800000031', '既有用户');
      const before = await readFile(filePath);

      const addDenied = await store.addWatchedUser(
        GROUP_A,
        STRANGER,
        '800000032',
        '越权添加',
      );
      const removeDenied = await store.removeWatchedUser(
        GROUP_A,
        STRANGER,
        '800000031',
      );
      const listed = store.listWatchedUsers(GROUP_A);
      const after = await readFile(filePath);

      expect(addDenied).toEqual({
        ok: false,
        message: '没有权限，该操作仅管理员可用',
      });
      expect(removeDenied).toEqual({
        ok: false,
        message: '没有权限，该操作仅管理员可用',
      });
      expect(listed).toEqual([{ uid: '800000031', name: '既有用户' }]);
      expect(after.equals(before)).toBe(true);
    });
  });

  it('配置损坏时特关增删拒绝，读取不暴露旧状态且损坏文件不被覆盖', async () => {
    await withTempDir(async (directory) => {
      const filePath = join(directory, 'subscriptions.json');
      const broken = Buffer.from('{"version":1,"groups":{BROKEN', 'utf8');
      await writeFile(filePath, broken);
      const store = new SubscriptionStore({
        filePath,
        bootstrapAdmins: [ADMIN],
      });

      await expect(store.load()).rejects.toThrow(SubscriptionsFileError);
      expect(store.loaded).toBe(false);

      const added = await store.addWatchedUser(
        GROUP_A,
        ADMIN,
        '800000041',
        '甲',
      );
      const removed = await store.removeWatchedUser(
        GROUP_A,
        ADMIN,
        '800000041',
      );

      expect(added).toEqual({
        ok: false,
        message: '订阅配置损坏，已停止写入以避免覆盖',
      });
      expect(removed).toEqual({
        ok: false,
        message: '订阅配置损坏，已停止写入以避免覆盖',
      });
      expect(store.listWatchedUsers(GROUP_A)).toEqual([]);
      expect((await readFile(filePath)).equals(broken)).toBe(true);
    });
  });

  it('sc 新订阅默认关闭；setNotify sc=true 后内存、磁盘与 reload 均为 true', async () => {
    await withTempDir(async (directory) => {
      const { filePath, store } = await freshStore(directory);
      await store.addAnchor(GROUP_A, ADMIN, '700000041', 'SC测试主播');

      expect(store.listAnchors(GROUP_A)[0]?.notify.sc).toBe(false);

      const updated = await store.setNotify(
        GROUP_A,
        ADMIN,
        '700000041',
        { sc: true },
      );
      expect(updated.ok).toBe(true);
      expect(updated.message).toContain('sc=on');
      expect(store.listAnchors(GROUP_A)[0]?.notify.sc).toBe(true);

      const onDisk = JSON.parse(await readFile(filePath, 'utf8')) as {
        groups: Record<
          string,
          { anchors: Record<string, { notify: { sc: boolean } }> }
        >;
      };
      expect(onDisk.groups[GROUP_A]?.anchors['700000041']?.notify.sc).toBe(true);

      const reloaded = new SubscriptionStore({ filePath });
      await reloaded.load();
      expect(reloaded.listAnchors(GROUP_A)[0]?.notify.sc).toBe(true);
    });
  });

  it('新增特关写操作与既有 addAnchor 并发时共用同一串行写队列且不丢更新', async () => {
    await withTempDir(async (directory) => {
      const { filePath, store } = await freshStore(directory);

      type AtomicWriteAccess = { atomicWrite: () => Promise<void> };
      const internals = store as unknown as AtomicWriteAccess;
      const realAtomicWrite = internals.atomicWrite.bind(store);
      let inflight = 0;
      let maxInflight = 0;

      internals.atomicWrite = async () => {
        inflight += 1;
        maxInflight = Math.max(maxInflight, inflight);
        await new Promise((resolve) => setTimeout(resolve, 20));
        try {
          await realAtomicWrite();
        } finally {
          inflight -= 1;
        }
      };

      const [watchedResult, anchorResult] = await Promise.all([
        store.addWatchedUser(GROUP_A, ADMIN, '800000051', '并发特关'),
        store.addAnchor(GROUP_A, ADMIN, '700000051', '并发主播'),
      ]);

      expect(watchedResult.ok).toBe(true);
      expect(anchorResult.ok).toBe(true);
      expect(maxInflight).toBe(1);
      expect(store.listWatchedUsers(GROUP_A)).toEqual([
        { uid: '800000051', name: '并发特关' },
      ]);
      expect(store.listAnchors(GROUP_A).map((anchor) => anchor.roomId)).toEqual([
        '700000051',
      ]);

      const onDisk = JSON.parse(await readFile(filePath, 'utf8')) as {
        groups: Record<
          string,
          {
            watchedUsers: Array<{ uid: string; name: string }>;
            anchors: Record<string, unknown>;
          }
        >;
      };
      expect(onDisk.groups[GROUP_A]?.watchedUsers).toEqual([
        { uid: '800000051', name: '并发特关' },
      ]);
      expect(Object.keys(onDisk.groups[GROUP_A]?.anchors ?? {})).toEqual([
        '700000051',
      ]);
    });
  });
});

/**
 * `#特关添加` 的参数解析（需求 R-17：UID 必需、显示名可选）。
 *
 * 抽成 `subscriptions.ts` 的纯函数而不是写在 `index.ts` 里，依据决策 D-54：
 * 接线层没有单测，凡是"可判定的逻辑"都应当留在有测试覆盖的一侧。
 * 显示名是**用户可控文本**，会进 `subscriptions.json` 并最终出现在群消息里，
 * 所以必须经 `oneLine()` 压单行并截断——下面第 5、6 条就是专门锁这一点的。
 */
describe('T-402 Batch 1：特关参数解析 parseWatchedUserInput', () => {
  it('只给 UID → 显示名用 UID 占位，不允许出现空名或 undefined', () => {
    expect(parseWatchedUserInput('800000077')).toEqual({
      ok: true,
      uid: '800000077',
      name: '800000077',
    });
  });

  it('UID + 显示名 → 用给定名字', () => {
    expect(parseWatchedUserInput('800000077 小恩')).toEqual({
      ok: true,
      uid: '800000077',
      name: '小恩',
    });
  });

  it('显示名含空格 → 合并为完整名字（允许带空格的名字）', () => {
    expect(parseWatchedUserInput('800000077 某 个 用户')).toEqual({
      ok: true,
      uid: '800000077',
      name: '某 个 用户',
    });
  });

  it('前后多余空白被忽略', () => {
    expect(parseWatchedUserInput('  800000077   小恩  ')).toEqual({
      ok: true,
      uid: '800000077',
      name: '小恩',
    });
  });

  it('★ 显示名含换行 → 必须被压成单行（否则会破坏 #特关列表 的排版）', () => {
    const parsed = parseWatchedUserInput('800000077 第一行\n第二行');
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(parsed.name).toBe('第一行 第二行');
    expect(parsed.name).not.toContain('\n');
  });

  it('★ 超长显示名被截断并加省略号（长度 = 上限 + 1，省略号本身占 1）', () => {
    const longName = '很长的名字'.repeat(20);
    const parsed = parseWatchedUserInput(`800000077 ${longName}`);
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(parsed.name.endsWith('…')).toBe(true);
    expect(Array.from(parsed.name).length).toBe(WATCHED_NAME_MAX_LENGTH + 1);
  });

  it('UID 不是纯数字 → 明确失败，不给默认值蒙混过关', () => {
    for (const bad of ['', '   ', 'abc', '8000000a7', '-800000077', '小恩']) {
      expect(parseWatchedUserInput(bad)).toEqual({ ok: false, reason: 'uid' });
    }
  });

  it('UID 带前导零也接受（不做数值化，避免丢前导零）', () => {
    expect(parseWatchedUserInput('000077')).toEqual({
      ok: true,
      uid: '000077',
      name: '000077',
    });
  });
});
