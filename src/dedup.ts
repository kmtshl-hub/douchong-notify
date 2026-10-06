export const DEDUP_PREFIX = 'qqnotify:dedup';
export const DEDUP_TTL_SECONDS = 86400;

export type NotifyEventType =
  | 'dynamic'
  | 'live'
  | 'liveEnd'
  | 'sc'
  | 'guestEntry';

/** 键格式：qqnotify:dedup:{event_type}:{room_id}:{event_id} */
export function dedupKey(
  eventType: NotifyEventType,
  roomId: number | string,
  eventId: string | number,
): string {
  return `${DEDUP_PREFIX}:${eventType}:${String(roomId)}:${String(eventId)}`;
}

/** SET NX EX 语义：键此前不存在 → 写入并返回 true；已存在 → 返回 false 且不改 TTL */
export interface DedupStore {
  setIfAbsent(key: string, ttlSeconds: number): Promise<boolean>;
}

/** 供测试与本地运行使用；必须真正遵守 TTL 过期 */
export function createMemoryDedupStore(options?: {
  now?: () => number;
}): DedupStore & { size(): number; has(key: string): boolean } {
  const now = options?.now ?? Date.now;
  const expirations = new Map<string, number>();

  const purgeExpired = (): void => {
    const currentTime = now();

    for (const [key, expiresAt] of expirations) {
      if (expiresAt <= currentTime) {
        expirations.delete(key);
      }
    }
  };

  return {
    async setIfAbsent(key: string, ttlSeconds: number): Promise<boolean> {
      const currentTime = now();
      const existingExpiration = expirations.get(key);

      if (
        existingExpiration !== undefined &&
        existingExpiration > currentTime
      ) {
        return false;
      }

      if (existingExpiration !== undefined) {
        expirations.delete(key);
      }

      expirations.set(key, currentTime + ttlSeconds * 1000);
      return true;
    },

    size(): number {
      purgeExpired();
      return expirations.size;
    },

    has(key: string): boolean {
      const expiration = expirations.get(key);
      if (expiration === undefined) {
        return false;
      }

      if (expiration <= now()) {
        expirations.delete(key);
        return false;
      }

      return true;
    },
  };
}

export interface Deduper {
  /** 首次见到该事件 → false；重复出现 → true */
  isDuplicate(
    eventType: NotifyEventType,
    roomId: number | string,
    eventId: string | number,
  ): Promise<boolean>;
}

export function createDeduper(
  store: DedupStore,
  ttlSeconds: number = DEDUP_TTL_SECONDS,
): Deduper {
  return {
    async isDuplicate(
      eventType: NotifyEventType,
      roomId: number | string,
      eventId: string | number,
    ): Promise<boolean> {
      const inserted = await store.setIfAbsent(
        dedupKey(eventType, roomId, eventId),
        ttlSeconds,
      );

      return !inserted;
    },
  };
}
