import { createServer, type Server, type ServerResponse } from 'node:http';

import type { SubscriptionStore } from './subscriptions.js';

const DEFAULT_NOTIFY_API_HOST = '127.0.0.1';
const DEFAULT_NOTIFY_API_PORT = 14667;

type NotifyApiStore = Pick<SubscriptionStore, 'loaded' | 'snapshot'>;

export type StartNotifyApiOptions = {
  store: NotifyApiStore;
  /** 测试可传 0 让系统分配随机端口；生产默认读取 NOTIFY_API_PORT。 */
  port?: number;
  /** 测试可显式指定绑定地址；生产默认读取 NOTIFY_API_HOST。 */
  host?: string;
};

function sendJson(
  response: ServerResponse,
  statusCode: number,
  body: unknown,
): void {
  const json = JSON.stringify(body);
  response.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
  });
  response.end(json);
}

/**
 * 解析监听地址。
 *
 * ⚠️ **为什么这个必须可配置**（2026-10-05 实测得到的结论）：
 * 最初按 D-61 硬编码 `127.0.0.1`，但那要求"斗虫能连到宿主机的回环地址"——
 * 而斗虫是**桥接容器**，它连宿主机网桥网关（172.19.0.1）都会被 iptables 丢包（实测 timeout），
 * 唯一可达的路径是**容器到容器**（斗虫 → `snowluma:3000` 已验证）。
 * 所以本服务改为跟随 qqbot-core 一起进入 docker 网络，绑定 `0.0.0.0`：
 * **该网络未向宿主机发布任何端口，因此不等于"对公网开放"**。
 *
 * 默认仍是 `127.0.0.1`（最小暴露面）；只有在私有容器网络里才应显式设为 `0.0.0.0`。
 */
function resolveHost(explicitHost: string | undefined): string {
  const raw = (explicitHost ?? process.env.NOTIFY_API_HOST ?? '').trim();
  return raw === '' ? DEFAULT_NOTIFY_API_HOST : raw;
}

function resolvePort(explicitPort: number | undefined): number {
  if (explicitPort !== undefined) {
    if (
      !Number.isInteger(explicitPort) ||
      explicitPort < 0 ||
      explicitPort > 65535
    ) {
      throw new Error('NOTIFY_API_PORT 必须是 0 到 65535 的整数');
    }
    return explicitPort;
  }

  const rawPort = process.env.NOTIFY_API_PORT?.trim();
  if (!rawPort) {
    return DEFAULT_NOTIFY_API_PORT;
  }

  const parsed = Number(rawPort);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error('NOTIFY_API_PORT 必须是 1 到 65535 的整数');
  }

  return parsed;
}

/**
 * 启动供斗虫只读拉取订阅快照的本机 HTTP 服务。
 *
 * 未配置 NOTIFY_API_TOKEN 时 fail closed：打印明确警告并返回 null，绝不会无鉴权启动。
 *
 * 绑定地址由 `NOTIFY_API_HOST` 决定，默认 `127.0.0.1`；只有在私有容器网络内
 * 才应显式设为 `0.0.0.0`（理由见 `resolveHost` 的注释）。
 */
export async function startNotifyApi(
  options: StartNotifyApiOptions,
): Promise<Server | null> {
  const token = process.env.NOTIFY_API_TOKEN?.trim();

  if (!token) {
    console.warn(
      JSON.stringify({
        type: 'notify_api_warning',
        reason: 'NOTIFY_API_TOKEN_missing',
        message: 'NOTIFY_API_TOKEN 未配置，通知订阅接口未启动',
      }),
    );
    return null;
  }

  const host = resolveHost(options.host);
  const port = resolvePort(options.port);
  const server = createServer((request, response) => {
    let pathname: string;
    try {
      // 只用来解析 request.url 的 pathname；此时还没读到 Host 头，用常量占位即可，
      // 与真实监听地址无关（换过地址不影响这里的语义）。
      pathname = new URL(request.url || '/', 'http://127.0.0.1').pathname;
    } catch {
      sendJson(response, 404, {
        error: 'not_found',
        message: '请求路径不存在',
      });
      return;
    }

    if (request.method !== 'GET' || pathname !== '/subscriptions') {
      sendJson(response, 404, {
        error: 'not_found',
        message: '请求路径不存在',
      });
      return;
    }

    if (request.headers.authorization !== `Bearer ${token}`) {
      sendJson(response, 401, {
        error: 'unauthorized',
        message: '缺少或错误的 Authorization Bearer token',
      });
      return;
    }

    if (!options.store.loaded) {
      sendJson(response, 503, {
        error: 'subscriptions_unavailable',
        message: '订阅配置尚未成功装载',
      });
      return;
    }

    try {
      // snapshot() 自身返回深拷贝；这里只序列化该快照，不暴露 store 内部引用。
      sendJson(response, 200, options.store.snapshot());
    } catch (error: unknown) {
      console.error(
        JSON.stringify({
          type: 'notify_api_request_error',
          reason: error instanceof Error ? error.message : 'unknown error',
        }),
      );
      sendJson(response, 500, {
        error: 'snapshot_failed',
        message: '订阅快照生成失败',
      });
    }
  });

  await new Promise<void>((resolve, reject) => {
    const handleStartupError = (error: Error): void => {
      reject(error);
    };

    server.once('error', handleStartupError);
    server.listen(port, host, () => {
      server.off('error', handleStartupError);
      resolve();
    });
  });

  const address = server.address();
  const actualPort =
    typeof address === 'object' && address !== null ? address.port : port;
  console.log(
    JSON.stringify({
      type: 'notify_api_started',
      host,
      port: actualPort,
    }),
  );

  server.on('error', (error) => {
    console.error(
      JSON.stringify({
        type: 'notify_api_error',
        error: error.message,
      }),
    );
  });

  return server;
}
