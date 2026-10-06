import { mkdir, open, readFile, rename, unlink } from 'node:fs/promises';
import { dirname } from 'node:path';

// 复用「用户可控文本」的唯一出口（决策 D-45/D-46）：特关显示名会被写进群消息，
// 必须压单行并截断。这里不自己写一份 `replace(/[\r\n]/)`——重复实现必然各自漂移。
import { oneLine } from './session-report.js';

export const SUBSCRIPTIONS_VERSION = 2;

export type NotifyFlags = {
  dynamic: boolean;
  live: boolean;
  liveEnd: boolean;
  sc: boolean;
  atAll: boolean;
};

export const DEFAULT_NOTIFY: NotifyFlags = {
  dynamic: true,
  live: true,
  liveEnd: true,
  sc: false,
  atAll: false,
};

export type WatchedUser = {
  uid: string;
  name: string;
};

export type AnchorSubscription = {
  name: string;
  notify: NotifyFlags;
};

export type GroupSubscriptions = {
  admins: string[];
  guestEntryEnabled: boolean;
  watchedUsers: WatchedUser[];
  anchors: Record<string, AnchorSubscription>;
};

export type SubscriptionsFile = {
  version: number;
  groups: Record<string, GroupSubscriptions>;
};

export type OpResult =
  | { ok: true; message: string }
  | { ok: false; message: string };

export type SubscriptionStoreOptions = {
  filePath: string;
  /** 环境变量 BOT_ADMIN_IDS 提供的初始管理员，作为兜底（配置里没写也能管） */
  bootstrapAdmins?: string[];
  /** 仅供测试隔离：替换原子写的 open 实现，默认使用 node:fs/promises.open。 */
  fileOperations?: {
    open?: typeof open;
  };
};

export class SubscriptionsFileError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = 'SubscriptionsFileError';
  }
}

const INVALID_PARAMETER_RESULT: OpResult = {
  ok: false,
  message: '参数格式不正确',
};

const NO_PERMISSION_RESULT: OpResult = {
  ok: false,
  message: '没有权限，该操作仅管理员可用',
};

const DAMAGED_FILE_RESULT: OpResult = {
  ok: false,
  message: '订阅配置损坏，已停止写入以避免覆盖',
};

const WRITE_FAILED_RESULT: OpResult = {
  ok: false,
  message: '配置写入失败，请稍后再试',
};

const NOT_SUBSCRIBED_RESULT: OpResult = {
  ok: false,
  message: '该主播不在本群订阅列表中',
};

const NUMERIC_ID = /^\d+$/;
const TRANSIENT_RENAME_CODES = new Set(['EPERM', 'EBUSY', 'EACCES']);

async function renameWithRetry(source: string, target: string): Promise<void> {
  const maxAttempts = 4;

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      await rename(source, target);
      return;
    } catch (error: unknown) {
      const code =
        isRecord(error) && typeof error.code === 'string' ? error.code : '';

      if (!TRANSIENT_RENAME_CODES.has(code) || attempt === maxAttempts) {
        throw error;
      }

      await new Promise((resolve) => setTimeout(resolve, attempt * 20));
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNumericId(value: string): boolean {
  return value.length > 0 && NUMERIC_ID.test(value);
}

/** 特关显示名长度上限（含省略号占位，取 `oneLine` 的 max 语义）。 */
export const WATCHED_NAME_MAX_LENGTH = 32;

export type WatchedUserInput =
  | { ok: true; uid: string; name: string }
  | { ok: false; reason: 'uid' };

/**
 * 解析 `#特关添加` 的参数：`<UID> [显示名]`。
 *
 * - **UID 必需**且必须是纯数字：它既是名单主键，也是通知去重键
 *   `qqnotify:dedup:guestEntry:{room_id}:{uid}:{session_id}` 的一部分（R-17）
 * - **显示名可选**：省略时用 UID 占位。需求 R-17 的用户可见文案是
 *   「已添加特关用户：<名字>（<UID>）」——取不到名字时不能让这里变成空串或 `undefined`
 *
 * ⚠️ 显示名是**用户可控文本**：它会被写进 `subscriptions.json` 并最终出现在群消息里，
 * 所以必须先经 `oneLine()` 压成单行并截断（决策 D-45 的"唯一出口"原则），
 * 否则一个带换行的名字就能破坏 `#特关列表` 的排版。
 *
 * 抽成纯函数而不是写在 `index.ts` 里，是决策 D-54：接线层没有单测，
 * 这类可判定的解析逻辑应当留在有测试覆盖的一侧。
 */
export function parseWatchedUserInput(input: string): WatchedUserInput {
  const tokens = input.trim().split(/\s+/u).filter(Boolean);
  const uid = tokens[0] ?? '';

  if (!isNumericId(uid)) {
    return { ok: false, reason: 'uid' };
  }

  const rawName = tokens.slice(1).join(' ').trim();
  return {
    ok: true,
    uid,
    name:
      rawName === '' ? uid : oneLine(rawName, WATCHED_NAME_MAX_LENGTH),
  };
}

function cloneNotify(notify: NotifyFlags): NotifyFlags {
  return {
    dynamic: notify.dynamic,
    live: notify.live,
    liveEnd: notify.liveEnd,
    sc: notify.sc,
    atAll: notify.atAll,
  };
}

function normalizeNotify(value: unknown): NotifyFlags {
  if (!isRecord(value)) {
    return cloneNotify(DEFAULT_NOTIFY);
  }

  return {
    dynamic:
      typeof value.dynamic === 'boolean'
        ? value.dynamic
        : DEFAULT_NOTIFY.dynamic,
    live:
      typeof value.live === 'boolean'
        ? value.live
        : DEFAULT_NOTIFY.live,
    liveEnd:
      typeof value.liveEnd === 'boolean'
        ? value.liveEnd
        : DEFAULT_NOTIFY.liveEnd,
    sc:
      typeof value.sc === 'boolean'
        ? value.sc
        : DEFAULT_NOTIFY.sc,
    atAll:
      typeof value.atAll === 'boolean'
        ? value.atAll
        : DEFAULT_NOTIFY.atAll,
  };
}

function normalizeGroupSubscriptions(
  groupId: string,
  value: unknown,
): GroupSubscriptions {
  if (!isRecord(value)) {
    throw new SubscriptionsFileError(`群 ${groupId} 的配置结构无效`);
  }

  let admins: string[] = [];
  if (value.admins !== undefined) {
    if (
      !Array.isArray(value.admins) ||
      !value.admins.every(
        (admin): admin is string =>
          typeof admin === 'string' && isNumericId(admin),
      )
    ) {
      throw new SubscriptionsFileError(`群 ${groupId} 的管理员列表无效`);
    }
    admins = [...value.admins];
  }

  const guestEntryEnabled =
    typeof value.guestEntryEnabled === 'boolean'
      ? value.guestEntryEnabled
      : false;

  const watchedUsers: WatchedUser[] = [];
  if (value.watchedUsers !== undefined) {
    if (!Array.isArray(value.watchedUsers)) {
      throw new SubscriptionsFileError(`群 ${groupId} 的特关用户列表无效`);
    }

    const seenUids = new Set<string>();
    for (const rawUser of value.watchedUsers) {
      if (
        !isRecord(rawUser) ||
        typeof rawUser.uid !== 'string' ||
        !isNumericId(rawUser.uid) ||
        typeof rawUser.name !== 'string' ||
        rawUser.name.trim().length === 0
      ) {
        throw new SubscriptionsFileError(`群 ${groupId} 的特关用户配置无效`);
      }

      if (!seenUids.has(rawUser.uid)) {
        watchedUsers.push({
          uid: rawUser.uid,
          name: rawUser.name.trim(),
        });
        seenUids.add(rawUser.uid);
      }
    }
  }

  if (!isRecord(value.anchors)) {
    throw new SubscriptionsFileError(`群 ${groupId} 的主播订阅结构无效`);
  }

  const anchors: Record<string, AnchorSubscription> = {};

  for (const [roomId, rawAnchor] of Object.entries(value.anchors)) {
    if (!isRecord(rawAnchor)) {
      throw new SubscriptionsFileError(
        `群 ${groupId} 的主播 ${roomId} 配置无效`,
      );
    }

    if (
      typeof rawAnchor.name !== 'string' ||
      rawAnchor.name.trim().length === 0
    ) {
      throw new SubscriptionsFileError(
        `群 ${groupId} 的主播 ${roomId} 名称无效`,
      );
    }

    anchors[roomId] = {
      name: rawAnchor.name,
      notify: normalizeNotify(rawAnchor.notify),
    };
  }

  return {
    admins,
    guestEntryEnabled,
    watchedUsers,
    anchors,
  };
}

function parseSubscriptionsFile(value: unknown): SubscriptionsFile {
  if (!isRecord(value)) {
    throw new SubscriptionsFileError('订阅配置顶层结构无效');
  }

  if (typeof value.version !== 'number' || !Number.isFinite(value.version)) {
    throw new SubscriptionsFileError('订阅配置 version 无效');
  }

  if (!isRecord(value.groups)) {
    throw new SubscriptionsFileError('订阅配置 groups 无效');
  }

  const groups: Record<string, GroupSubscriptions> = {};

  for (const [groupId, rawGroup] of Object.entries(value.groups)) {
    groups[groupId] = normalizeGroupSubscriptions(groupId, rawGroup);
  }

  return {
    version: SUBSCRIPTIONS_VERSION,
    groups,
  };
}

function cloneSubscriptionsFile(source: SubscriptionsFile): SubscriptionsFile {
  const groups: Record<string, GroupSubscriptions> = {};

  for (const [groupId, group] of Object.entries(source.groups)) {
    const anchors: Record<string, AnchorSubscription> = {};

    for (const [roomId, anchor] of Object.entries(group.anchors)) {
      anchors[roomId] = {
        name: anchor.name,
        notify: cloneNotify(anchor.notify),
      };
    }

    groups[groupId] = {
      admins: [...group.admins],
      guestEntryEnabled: group.guestEntryEnabled,
      watchedUsers: group.watchedUsers.map((user) => ({ ...user })),
      anchors,
    };
  }

  return {
    version: source.version,
    groups,
  };
}

export class SubscriptionStore {
  private readonly filePath: string;
  private readonly bootstrapAdmins: Set<string>;
  private readonly openFile: typeof open;
  private data: SubscriptionsFile = {
    version: SUBSCRIPTIONS_VERSION,
    groups: {},
  };
  private isLoaded = false;
  private writeQueue: Promise<void> = Promise.resolve();

  constructor(options: SubscriptionStoreOptions) {
    this.filePath = options.filePath;
    this.bootstrapAdmins = new Set(options.bootstrapAdmins ?? []);
    this.openFile = options.fileOperations?.open ?? open;
  }

  get loaded(): boolean {
    return this.isLoaded;
  }

  async load(): Promise<void> {
    this.isLoaded = false;

    let text: string;
    try {
      text = await readFile(this.filePath, 'utf8');
    } catch (error: unknown) {
      if (
        isRecord(error) &&
        typeof error.code === 'string' &&
        error.code === 'ENOENT'
      ) {
        this.data = {
          version: SUBSCRIPTIONS_VERSION,
          groups: {},
        };
        this.isLoaded = true;
        return;
      }

      throw new SubscriptionsFileError('无法读取订阅配置', {
        cause: error,
      });
    }

    try {
      const parsed: unknown = JSON.parse(text);
      this.data = parseSubscriptionsFile(parsed);
      this.isLoaded = true;
    } catch (error: unknown) {
      this.isLoaded = false;

      if (error instanceof SubscriptionsFileError) {
        throw error;
      }

      throw new SubscriptionsFileError('订阅配置不是有效的 JSON', {
        cause: error,
      });
    }
  }

  snapshot(): SubscriptionsFile {
    return cloneSubscriptionsFile(this.data);
  }

  isAdmin(groupId: string, userId: string): boolean {
    if (this.bootstrapAdmins.has(userId)) {
      return true;
    }

    const group = this.data.groups[groupId];
    return group !== undefined && group.admins.includes(userId);
  }

  listAdmins(groupId: string): string[] {
    const group = this.data.groups[groupId];
    return group === undefined ? [] : [...group.admins];
  }

  listAnchors(
    groupId: string,
  ): Array<{ roomId: string; name: string; notify: NotifyFlags }> {
    const group = this.data.groups[groupId];
    if (group === undefined) {
      return [];
    }

    return Object.entries(group.anchors)
      .map(([roomId, anchor]) => ({
        roomId,
        name: anchor.name,
        notify: cloneNotify(anchor.notify),
      }))
      .sort((left, right) => {
        const leftNumber = Number(left.roomId);
        const rightNumber = Number(right.roomId);

        if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
          return leftNumber - rightNumber;
        }

        return left.roomId.localeCompare(right.roomId);
      });
  }

  listWatchedUsers(groupId: string): WatchedUser[] {
    if (!this.isLoaded) {
      return [];
    }

    const group = this.data.groups[groupId];
    return group === undefined
      ? []
      : group.watchedUsers.map((user) => ({ ...user }));
  }

  async addAnchor(
    groupId: string,
    userId: string,
    roomId: string,
    name: string,
  ): Promise<OpResult> {
    if (!this.isLoaded) {
      return DAMAGED_FILE_RESULT;
    }

    if (
      !isNumericId(groupId) ||
      !isNumericId(userId) ||
      !isNumericId(roomId)
    ) {
      return INVALID_PARAMETER_RESULT;
    }

    if (!this.isAdmin(groupId, userId)) {
      return NO_PERMISSION_RESULT;
    }

    const normalizedName = name.trim();
    if (normalizedName.length === 0) {
      return {
        ok: false,
        message: '主播名称不能为空',
      };
    }

    return this.enqueueMutation(async () => {
      const existing = this.data.groups[groupId]?.anchors[roomId];
      if (existing !== undefined) {
        return {
          ok: true,
          message: `该主播已在本群订阅列表中：${existing.name}（${roomId}）`,
        };
      }

      const before = cloneSubscriptionsFile(this.data);
      const group = this.ensureGroup(groupId);
      group.anchors[roomId] = {
        name: normalizedName,
        notify: cloneNotify(DEFAULT_NOTIFY),
      };

      if (!(await this.persistOrRollback(before))) {
        return WRITE_FAILED_RESULT;
      }

      return {
        ok: true,
        message: `已添加主播订阅：${normalizedName}（${roomId}）`,
      };
    });
  }

  async removeAnchor(
    groupId: string,
    userId: string,
    roomId: string,
  ): Promise<OpResult> {
    if (!this.isLoaded) {
      return DAMAGED_FILE_RESULT;
    }

    if (
      !isNumericId(groupId) ||
      !isNumericId(userId) ||
      !isNumericId(roomId)
    ) {
      return INVALID_PARAMETER_RESULT;
    }

    if (!this.isAdmin(groupId, userId)) {
      return NO_PERMISSION_RESULT;
    }

    return this.enqueueMutation(async () => {
      const group = this.data.groups[groupId];
      const anchor = group?.anchors[roomId];

      if (group === undefined || anchor === undefined) {
        return NOT_SUBSCRIBED_RESULT;
      }

      const before = cloneSubscriptionsFile(this.data);
      delete group.anchors[roomId];

      if (!(await this.persistOrRollback(before))) {
        return WRITE_FAILED_RESULT;
      }

      return {
        ok: true,
        message: `已移除订阅：${anchor.name}（${roomId}）`,
      };
    });
  }

  async setNotify(
    groupId: string,
    userId: string,
    roomId: string,
    patch: Partial<NotifyFlags>,
  ): Promise<OpResult> {
    if (!this.isLoaded) {
      return DAMAGED_FILE_RESULT;
    }

    if (
      !isNumericId(groupId) ||
      !isNumericId(userId) ||
      !isNumericId(roomId)
    ) {
      return INVALID_PARAMETER_RESULT;
    }

    if (!this.isAdmin(groupId, userId)) {
      return NO_PERMISSION_RESULT;
    }

    return this.enqueueMutation(async () => {
      const anchor = this.data.groups[groupId]?.anchors[roomId];
      if (anchor === undefined) {
        return NOT_SUBSCRIBED_RESULT;
      }

      const before = cloneSubscriptionsFile(this.data);

      if (typeof patch.dynamic === 'boolean') {
        anchor.notify.dynamic = patch.dynamic;
      }
      if (typeof patch.live === 'boolean') {
        anchor.notify.live = patch.live;
      }
      if (typeof patch.liveEnd === 'boolean') {
        anchor.notify.liveEnd = patch.liveEnd;
      }
      if (typeof patch.sc === 'boolean') {
        anchor.notify.sc = patch.sc;
      }
      if (typeof patch.atAll === 'boolean') {
        anchor.notify.atAll = patch.atAll;
      }

      if (!(await this.persistOrRollback(before))) {
        return WRITE_FAILED_RESULT;
      }

      const notify = anchor.notify;
      return {
        ok: true,
        message:
          `已更新通知设置：${anchor.name}` +
          ` dynamic=${notify.dynamic ? 'on' : 'off'}` +
          ` live=${notify.live ? 'on' : 'off'}` +
          ` liveEnd=${notify.liveEnd ? 'on' : 'off'}` +
          ` sc=${notify.sc ? 'on' : 'off'}` +
          ` atAll=${notify.atAll ? 'on' : 'off'}`,
      };
    });
  }

  async addWatchedUser(
    groupId: string,
    userId: string,
    targetUid: string,
    targetName: string,
  ): Promise<OpResult> {
    if (!this.isLoaded) {
      return DAMAGED_FILE_RESULT;
    }

    if (
      !isNumericId(groupId) ||
      !isNumericId(userId) ||
      !isNumericId(targetUid)
    ) {
      return INVALID_PARAMETER_RESULT;
    }

    if (!this.isAdmin(groupId, userId)) {
      return NO_PERMISSION_RESULT;
    }

    const normalizedName = targetName.trim();
    if (normalizedName.length === 0) {
      return {
        ok: false,
        message: '特关用户名不能为空',
      };
    }

    return this.enqueueMutation(async () => {
      const existing = this.data.groups[groupId]?.watchedUsers.find(
        (user) => user.uid === targetUid,
      );
      if (existing !== undefined) {
        return {
          ok: true,
          message: `已在特关列表中：${existing.name}（${targetUid}）`,
        };
      }

      const before = cloneSubscriptionsFile(this.data);
      const group = this.ensureGroup(groupId);
      group.watchedUsers.push({
        uid: targetUid,
        name: normalizedName,
      });

      if (!(await this.persistOrRollback(before))) {
        return WRITE_FAILED_RESULT;
      }

      return {
        ok: true,
        message: `已添加特关用户：${normalizedName}（${targetUid}）`,
      };
    });
  }

  async removeWatchedUser(
    groupId: string,
    userId: string,
    targetUid: string,
  ): Promise<OpResult> {
    if (!this.isLoaded) {
      return DAMAGED_FILE_RESULT;
    }

    if (
      !isNumericId(groupId) ||
      !isNumericId(userId) ||
      !isNumericId(targetUid)
    ) {
      return INVALID_PARAMETER_RESULT;
    }

    if (!this.isAdmin(groupId, userId)) {
      return NO_PERMISSION_RESULT;
    }

    return this.enqueueMutation(async () => {
      const group = this.data.groups[groupId];
      const index = group?.watchedUsers.findIndex(
        (user) => user.uid === targetUid,
      ) ?? -1;

      if (group === undefined || index < 0) {
        return {
          ok: false,
          message: `该特关用户不在本群列表中：${targetUid}`,
        };
      }

      const before = cloneSubscriptionsFile(this.data);
      group.watchedUsers.splice(index, 1);

      if (!(await this.persistOrRollback(before))) {
        return WRITE_FAILED_RESULT;
      }

      return {
        ok: true,
        message: `已移除特关用户：${targetUid}`,
      };
    });
  }

  /**
   * 把某个用户提升为本群管理员。
   *
   * ⚠️ 阶段 1 尚未接线为 QQ 命令（当前管理员来自 BOT_ADMIN_IDS 兜底或人工编辑配置文件）。
   * 计划在阶段 4「主播管理与多群订阅」里加 `#添加管理员` 命令调用它。
   * 在此之前它由测试覆盖，不是无人使用的死代码。
   */
  async addAdmin(
    groupId: string,
    userId: string,
    targetUserId: string,
  ): Promise<OpResult> {
    if (!this.isLoaded) {
      return DAMAGED_FILE_RESULT;
    }

    if (
      !isNumericId(groupId) ||
      !isNumericId(userId) ||
      !isNumericId(targetUserId)
    ) {
      return INVALID_PARAMETER_RESULT;
    }

    if (!this.isAdmin(groupId, userId)) {
      return NO_PERMISSION_RESULT;
    }

    return this.enqueueMutation(async () => {
      const existingGroup = this.data.groups[groupId];
      if (existingGroup?.admins.includes(targetUserId) === true) {
        return {
          ok: true,
          message: `已添加管理员：${targetUserId}`,
        };
      }

      const before = cloneSubscriptionsFile(this.data);
      const group = this.ensureGroup(groupId);
      group.admins.push(targetUserId);

      if (!(await this.persistOrRollback(before))) {
        return WRITE_FAILED_RESULT;
      }

      return {
        ok: true,
        message: `已添加管理员：${targetUserId}`,
      };
    });
  }

  private ensureGroup(groupId: string): GroupSubscriptions {
    const existing = this.data.groups[groupId];
    if (existing !== undefined) {
      return existing;
    }

    const created: GroupSubscriptions = {
      admins: [],
      guestEntryEnabled: false,
      watchedUsers: [],
      anchors: {},
    };
    this.data.groups[groupId] = created;
    return created;
  }

  private enqueueMutation<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.writeQueue.then(operation);
    this.writeQueue = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }

  private async persistOrRollback(
    previous: SubscriptionsFile,
  ): Promise<boolean> {
    try {
      await this.atomicWrite();
      return true;
    } catch {
      this.data = previous;
      return false;
    }
  }

  private async atomicWrite(): Promise<void> {
    const temporaryPath = `${this.filePath}.tmp`;
    let handle: Awaited<ReturnType<typeof open>> | undefined;

    try {
      await mkdir(dirname(this.filePath), { recursive: true });

      handle = await this.openFile(temporaryPath, 'w');
      const json = `${JSON.stringify(this.data, null, 2)}\n`;
      await handle.writeFile(json, 'utf8');
      await handle.sync();
      await handle.close();
      handle = undefined;

      await renameWithRetry(temporaryPath, this.filePath);
    } catch (error: unknown) {
      if (handle !== undefined) {
        try {
          await handle.close();
        } catch {
          // 保留原始写入错误。
        }
      }

      try {
        await unlink(temporaryPath);
      } catch {
        // 临时文件可能尚未创建，或 rename 已经完成。
      }

      throw error;
    }
  }
}
