import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

/**
 * ⚠️⚠️ 这两处设置在 Windows / WorkBuddy 沙箱里是**必需的**，删掉就会出诡异问题。
 *
 * 【问题】vitest 在跑测试时，会把每个被转换过的模块**写到磁盘**再由 Node import：
 *     node_modules/vitest/dist/chunks/cli-api.DqsSTaIi.js:9883
 *       tmpDir = join(tmpdir(), this.id)
 * 也就是写进**系统临时目录**（`%TEMP%`）。而本机沙箱的文件访问代理**不允许写工作区之外**，
 * 于是报 `EPERM: operation not permitted, open '...\Temp\<rand>\ssr\<hash>'`。
 *
 * 【为什么危害极大】这个错误是**异步**抛出的，vitest 把它记成 unhandled error，
 * 结果是：**那个模块对应的整个测试文件被静默丢弃**，而输出里只显示少了一个文件——
 * 曾经出现过「输出写着 12 passed、退出码却是 1」，另一个文件的 16 个用例**根本没跑**。
 * 换句话说：**测试会假装通过**。
 *
 * 【处置】
 *   1. `cacheDir`  → 管住 Vite 的转换缓存；
 *   2. 改写 `TMPDIR/TMP/TEMP` → 管住上面那个 `tmpDir`（必须早于 vitest 构造 project，
 *      所以放在配置文件顶层执行，且早于 `defineConfig`）。
 *      `os.tmpdir()` 读环境变量，指向仓库内的 `.tmp-vitest/`（已 gitignore）之后，
 *      临时读写就落在工作区里，代理解除拦截。
 *
 * 【自检办法】跑完测试务必核对两件事，缺一不可：
 *   - 「Test Files」的数量等于 `src/__tests__/` 下的文件数；
 *   - 退出码为 0。
 *   只看到「Tests N passed」是不够的——丢文件时用例数会跟着变小，但输出依然像成功。
 */
const projectRoot = fileURLToPath(new URL('.', import.meta.url));
const localTmp = join(projectRoot, '.tmp-vitest');
mkdirSync(localTmp, { recursive: true });
process.env.TMPDIR = localTmp;
process.env.TMP = localTmp;
process.env.TEMP = localTmp;

export default defineConfig({
  cacheDir: '.vite-cache',
  test: {
    include: ['src/**/*.test.ts'],
    // 阶段 1 起测试涉及真实文件读写，给足超时余量
    testTimeout: 15000,
    /**
     * ⚠️ 关掉测试文件级并行（实测有效，2026-10-05）。
     *
     * 【观察到的现象】打开并行时，`EPERM: operation not permitted, open '...\ssr\<hash>'`
     * 会间歇出现，且**同一个 hash 反复中招**；关掉并行后连跑多次不再复现。
     *
     * 【关于成因】我在 vitest 源码里读到过一条可疑路径（模块产物写盘前的 promises 去重表、
     * 条目写完后被 `.finally()` 清掉），但**这只是阅读源码得到的推测，无法从本仓库自证**，
     * 所以不写成结论。真正的判据只有一条：并行会间歇失败、串行不会（第三轮评审 F5）。
     * 若将来要恢复并行，请先连续跑 5 次以上确认 0 个 EPERM，再改这里。
     *
     * 【代价】本项目的测试文件只有个位数、跑完十几秒，串行完全可以接受。
     */
    fileParallelism: false,
  },
});

