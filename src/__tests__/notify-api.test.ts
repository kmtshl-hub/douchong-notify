import type { Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

import { startNotifyApi } from '../notify-api.js';
import type { SubscriptionsFile } from '../subscriptions.js';

const GROUP = '900000001';
const TEST_TOKEN = 'test-notify-token';

const sourceState: SubscriptionsFile = {
  version: 2,
  groups: {
    [GROUP]: {
      admins: ['800000001'],
      guestEntryEnabled: false,
      watchedUsers: [{ uid: '800000011', name: '特关甲' }],
      anchors: {
        '700000001': {
          name: '示例主播',
          notify: {
            dynamic: true,
            live: true,
            liveEnd: true,
            sc: false,
            atAll: false,
          },
        },
      },
    },
  },
};

const mockStore = {
  loaded: true,
  snapshot: (): SubscriptionsFile => structuredClone(sourceState),
};

let server: Server | null = null;
let baseUrl = '';
let originalToken: string | undefined;

beforeAll(async () => {
  originalToken = process.env.NOTIFY_API_TOKEN;
  process.env.NOTIFY_API_TOKEN = TEST_TOKEN;
  server = await startNotifyApi({ store: mockStore, port: 0 });

  if (server === null) {
    throw new Error('测试服务未启动');
  }

  const address = server.address() as AddressInfo | null;
  if (address === null) {
    throw new Error('测试服务没有监听地址');
  }
  baseUrl = `http://127.0.0.1:${address.port}`;
});

afterAll(async () => {
  if (server !== null) {
    await new Promise<void>((resolve, reject) => {
      server?.close((error) => {
        if (error) {
          reject(error);
          return;
        }
        resolve();
      });
    });
  }

  if (originalToken === undefined) {
    delete process.env.NOTIFY_API_TOKEN;
  } else {
    process.env.NOTIFY_API_TOKEN = originalToken;
  }
});

async function get(path: string, token?: string): Promise<Response> {
  const headers = token === undefined
    ? undefined
    : { Authorization: `Bearer ${token}` };
  return fetch(`${baseUrl}${path}`, { method: 'GET', headers });
}

describe('notify-api', () => {
  it('无 Authorization 时返回 401', async () => {
    const response = await get('/subscriptions');
    expect(response.status).toBe(401);
  });

  it('错误 token 时返回 401', async () => {
    const response = await get('/subscriptions', 'wrong-token');
    expect(response.status).toBe(401);
  });

  it('正确 token 时返回 200、JSON Content-Type 且 version 为 2', async () => {
    const response = await get('/subscriptions', TEST_TOKEN);
    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toContain('application/json');

    const body = (await response.json()) as SubscriptionsFile;
    expect(body.version).toBe(2);
  });

  it('store.loaded=false 时返回 503', async () => {
    mockStore.loaded = false;
    try {
      const response = await get('/subscriptions', TEST_TOKEN);
      expect(response.status).toBe(503);
    } finally {
      mockStore.loaded = true;
    }
  });

  it('未知路径返回 404', async () => {
    const response = await get('/unknown', TEST_TOKEN);
    expect(response.status).toBe(404);
  });

  it('响应内容与 store 状态隔离：修改客户端 JSON 不影响后续 snapshot', async () => {
    const response = await get('/subscriptions', TEST_TOKEN);
    expect(response.status).toBe(200);

    const body = (await response.json()) as SubscriptionsFile;
    const group = body.groups[GROUP];
    expect(group).toBeDefined();
    group?.watchedUsers.push({ uid: '800000099', name: '客户端改动' });
    if (group !== undefined) {
      group.anchors['700000001']!.notify.sc = true;
    }

    const after = mockStore.snapshot();
    expect(after.groups[GROUP]?.watchedUsers).toEqual([
      { uid: '800000011', name: '特关甲' },
    ]);
    expect(after.groups[GROUP]?.anchors['700000001']?.notify.sc).toBe(false);
  });

  it('未配置 NOTIFY_API_TOKEN 时明确拒绝启动，不能无鉴权开放', async () => {
    const savedToken = process.env.NOTIFY_API_TOKEN;
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    delete process.env.NOTIFY_API_TOKEN;

    try {
      const disabledServer = await startNotifyApi({ store: mockStore, port: 0 });
      expect(disabledServer).toBeNull();
      expect(warning).toHaveBeenCalledTimes(1);
      expect(warning.mock.calls[0]?.[0]).toContain('NOTIFY_API_TOKEN 未配置');
    } finally {
      warning.mockRestore();
      if (savedToken === undefined) {
        delete process.env.NOTIFY_API_TOKEN;
      } else {
        process.env.NOTIFY_API_TOKEN = savedToken;
      }
    }
  });
});

/**
 * 绑定地址（`NOTIFY_API_HOST`）——2026-10-05 在真实环境实测后补上的可配置项。
 *
 * 起因：D-61 原本硬编码 `127.0.0.1`，隐含前提是「斗虫能连到宿主机的回环地址」。
 * 实测发现**不成立**：斗虫是桥接容器，连宿主机网桥网关都会被 iptables 丢包（timeout），
 * 唯一可用路径是**容器到容器**。于是本服务改为跟随 qqbot-core 一起进 docker 网络、
 * 绑定 `0.0.0.0`——**该网络未向宿主机发布端口，因此不等于对公网开放**。
 *
 * 这组用例锁三件事：
 *   1. 默认仍是 `127.0.0.1`（最小暴露面，不显式配置就不会放开）
 *   2. `NOTIFY_API_HOST` 能被读到并真正生效（绑到哪个地址由 `server.address()` 证明）
 *   3. 显式入参优先于环境变量（测试可控性）
 */
describe('notify-api 绑定地址', () => {
  const savedToken = process.env.NOTIFY_API_TOKEN;
  const savedHost = process.env.NOTIFY_API_HOST;

  const close = async (instance: Server | null): Promise<void> => {
    if (instance === null) return;
    await new Promise<void>((resolve) => instance.close(() => resolve()));
  };

  const bindAddressOf = (instance: Server | null): string => {
    const address = instance?.address() as AddressInfo | null;
    return address === null || address === undefined ? '<none>' : address.address;
  };

  beforeAll(() => {
    process.env.NOTIFY_API_TOKEN = TEST_TOKEN;
  });

  afterAll(() => {
    if (savedToken === undefined) delete process.env.NOTIFY_API_TOKEN;
    else process.env.NOTIFY_API_TOKEN = savedToken;
    if (savedHost === undefined) delete process.env.NOTIFY_API_HOST;
    else process.env.NOTIFY_API_HOST = savedHost;
  });

  it('★ 未配置 NOTIFY_API_HOST 时默认只绑 127.0.0.1（不显式配置就不放开）', async () => {
    delete process.env.NOTIFY_API_HOST;
    const instance = await startNotifyApi({ store: mockStore, port: 0 });
    try {
      expect(bindAddressOf(instance)).toBe('127.0.0.1');
    } finally {
      await close(instance);
    }
  });

  it('★ NOTIFY_API_HOST=0.0.0.0 时真的绑到 0.0.0.0（为容器网络互通所需）', async () => {
    process.env.NOTIFY_API_HOST = '0.0.0.0';
    const instance = await startNotifyApi({ store: mockStore, port: 0 });
    try {
      expect(bindAddressOf(instance)).toBe('0.0.0.0');
    } finally {
      await close(instance);
    }
  });

  it('显式入参 host 优先于环境变量（测试可控性）', async () => {
    process.env.NOTIFY_API_HOST = '0.0.0.0';
    const instance = await startNotifyApi({
      store: mockStore,
      port: 0,
      host: '127.0.0.1',
    });
    try {
      expect(bindAddressOf(instance)).toBe('127.0.0.1');
    } finally {
      await close(instance);
    }
  });

  it('空白字符串等同于未配置，回落到默认值（不让空值把服务绑到意外地址）', async () => {
    process.env.NOTIFY_API_HOST = '   ';
    const instance = await startNotifyApi({ store: mockStore, port: 0 });
    try {
      expect(bindAddressOf(instance)).toBe('127.0.0.1');
    } finally {
      await close(instance);
    }
  });
});
