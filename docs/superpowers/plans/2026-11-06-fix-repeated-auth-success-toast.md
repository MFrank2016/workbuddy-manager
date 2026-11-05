# Fix repeated "授权成功" toast on add-account — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 加账号成功后不再无限弹「授权成功」toast，且同一个扫码 state 不会被并发供给/落盘两次；多窗口仍各自正常收尾。

**Architecture:** 三层同时加固。(A) 前端 `AddAccountDialog` 用「代次令牌 + 完成标记」让重入的 `start()` 与残留 tick 一律失效；(B) 服务端按 state 加 `asyncio.Lock`，把「ready 判定 → 供给 → 落盘」整段串行化；(C) 终态结果**仅对同一客户端会话**重复读时降级为 `already_handled`（前端静默收尾、不弹 toast），并让 TTL 清理在 `auth_poll` 里也能推进。

**Tech Stack:** Next 15 / React 19 / TypeScript（前端，静态导出 `web/out`）；FastAPI + asyncio（Python 3.12 容器 / 3.15 本机）；unittest（服务端）；Node 26 原生 `node:test` + TS 类型剥离（前端新测试底座）。

**Spec:** 无独立 spec 文档；本计划即需求依据。根因证据见会话记录：`data/manager.db` 里 `amendolagoodfellow@gmail.com` (uid `9094468f…`) 有两条 `source='add'` 的 `checkin_logs`（15:44:54、15:48:22）；`docker logs` 里 `state=5705a6b2…` 在落盘成功后仍被每 2 秒轮询 4 分钟；两次 `auth/start` 相隔 105ms。

## Global Constraints

- 全局红线：**任何情况下不提 PR**（`AGENTS.md`）。只提交到本地分支 / 告知分支名。
- 部署约定：容器非 root、端口绑 127.0.0.1、不挂 docker.sock、不替换系统 python3。
- 生成产物禁止手工合并：`web/out` 由 `cd web && NEXT_OUTPUT_EXPORT=1 npx --no-install next build` 从源重建，不手改压缩产物。
- 服务端测试命令：`cd <repo-root> && python3 -m unittest server.tests.<module> -v`（`-t .` 让 `server` 可导入；`tests/__init__.py` 的 guard 会拒绝真实数据目录）。
- 全量回归：`python3 -m unittest discover -s server/tests -t .`（本机 3.15；已有 17 个预先存在的失败，见 d372f42 提交说明，须逐一点名）。
- 前端类型检查：`cd web && npx --no-install tsc --noEmit`（目标 0 error）。
- 前端测试：`node --test web/lib/__tests__/*.test.mjs`（Node 26 原生 TS 剥离，`.mjs` 导入 `.ts`；不改 `web/package.json` 的 type）。
- 不新增生产依赖；`web/lib/poll-guard.ts` 必须是纯函数、无 React/DOM 依赖，才能被 `node:test` 直接跑。
- 复用现有测试夹具 `server.tests.test_login_server_poll.LoginPollTestCase`（`setUp/tearDown` 已清 `_login_polls` / `_login_tasks` / `_login_regions` / `_login_owner` / `_provisioned_states` / `_login_ready` / `tencent._state_cache`）。
- **多窗口契约不可破**：CHANGELOG 1.0.71「同一个授权在多开几个窗口时也只查一次」。C 的去重键必须按**客户端实例**（见 Task 3 的 `client` 参数），不能按全局 state —— 否则第二个窗口永远停在「等待授权完成」。
- 保持既有语义钉子不回归：`test_login_server_poll.py` 全部 10 条、`test_realm.py::test_retry_does_not_repeat_side_effects`、`test_global_login_activation.py` 全部 3 条。特别是「落盘失败 → 保留 state → 重试只补落盘、不重复签到」和「终态不再问腾讯第二次」。

## File Structure

| 文件                                                        | 责任                                                                                                                                                           |
| ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `web/lib/poll-guard.ts`（新建）                             | 纯逻辑：代次令牌 + 一次性完成标记 + 终态分类 `classifyTerminal(status, delivered)` → `'ignore' \| 'announce' \| 'quiet'`。与 React/DOM 解耦。                  |
| `web/lib/__tests__/poll-guard.test.mjs`（新建）             | 前端 RED 用例：重入使旧代失效、终态只播报一次、成功后 tick 不再执行、组件确实接入 guard。                                                                      |
| `web/components/common/accounts/AddAccountDialog.tsx`（改） | 接入 guard：`stopPoll` 递增代次；`start` await 后校验；`tick` 进入/await 后校验；success 先 `markDone`；`already_handled` 走静默收尾。                         |
| `server/tests/test_login_dedup.py`（新建）                  | 服务端 RED 用例：并发 auth_poll 只落盘一次；同一 client 重复读返回 `already_handled`、不同 client 仍得 success；poll 入口 prune 过期终态；锁在落盘失败后释放。 |
| `server/routers/accounts.py`（改）                          | 按 state 的 `asyncio.Lock`；锁内串行化 ready→供给→落盘；终态按交付者去重；poll 入口顺带 prune；`auth_poll` 新删参数 `client`（默认 `None` 兼容既有直呼）。     |
| `web/out/**`（重新生成）                                    | 由 `next build` 重建，不手改。                                                                                                                                 |

## Review Focus

1. **多标签页 / 多窗口 legit 读取**：两个浏览器窗口都该各自看到一次成功并各自关弹窗，不能因为第一个窗口「领走」终态就让第二个永远 waiting。→ 去重键用 `client`（前端 `sessionStorage` 里的随机 id，随 `poll` 查询参数上送）；测试 `test_second_window_still_gets_one_success` 钉住。
2. **落盘失败仍可重试**：加锁后 `HTTPException` 不能被锁/状态吞掉；`_login_ready` 与 state 保留语义不变；锁必须在异常路径释放。→ `test_save_failure_is_not_cached_so_retry_still_saves` 保持通过 + 新增 `test_lock_released_after_save_failure`。
3. **弹窗关闭/重开**：`open=false` 后再打开必须是全新代次，旧的泄漏 tick 不能复活；`realm`/`region`/`proxy` 连续变化不能各留一个 interval。→ 前端 `test_reopen_starts_fresh_generation`、`test_consecutive_reentries_leave_single_interval`。
4. **`auth_start` 在锁持有期间重入**：`_cancel_other_login_polls` 取消旧任务时不能与持锁的旧任务死锁（取消点在 `await asyncio.sleep` / `await auth_poll`）。→ `test_cancel_during_inflight_poll_does_not_deadlock`（`asyncio.wait_for(..., 5)`）。
5. **TTL 清理不依赖下一次发码**：不发新码时过期终态也要能被回收，否则 `_login_polls` 无限增长。→ `test_expired_terminal_is_pruned_on_poll`。

---

### Task 1: 前端 guard 纯逻辑（A 的可测内核）

**Files:**

- Create: `web/lib/poll-guard.ts`
- Test: `web/lib/__tests__/poll-guard.test.mjs`

**Interfaces:**

- Produces:
  - `createPollGuard()` → `{begin(): number, invalidate(): number, isStale(g): boolean, markDone(): void, isDone(): boolean}`
  - `classifyTerminal(status: string, delivered: boolean)` → `'ignore' | 'announce' | 'quiet'`
    - `'ignore'`：本轮已完成，彻底不管（`delivered === true` 或 `status === 'already_handled'` 且本轮已收尾）
    - `'announce'`：正常成功 → 弹 toast、关弹窗、刷列表
    - `'quiet'`：服务端说这份终态已被**本客户端**消费 → 静默关弹窗 + 刷列表，**不弹 toast**

- [ ] **Step 1: 写失败测试** `web/lib/__tests__/poll-guard.test.mjs`

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { createPollGuard, classifyTerminal } from "../poll-guard.ts";

test("re-entry invalidates the previous generation", () => {
  const g = createPollGuard();
  const first = g.begin();
  const second = g.begin();
  assert.equal(g.isStale(first), true, "旧代必须失效");
  assert.equal(g.isStale(second), false, "最新代必须有效");
});

test("invalidate (stopPoll/close) makes every in-flight tick stale", () => {
  const g = createPollGuard();
  const g1 = g.begin();
  g.invalidate();
  assert.equal(g.isStale(g1), true);
});

test("consecutive re-entries leave exactly one active generation", () => {
  const g = createPollGuard();
  const generations = [g.begin(), g.begin(), g.begin()];
  assert.equal(generations.filter((x) => !g.isStale(x)).length, 1);
});

test("reopen after invalidate starts a fresh usable generation", () => {
  const g = createPollGuard();
  g.begin();
  g.invalidate();
  const fresh = g.begin();
  assert.equal(g.isStale(fresh), false);
});

test("markDone is one-way", () => {
  const g = createPollGuard();
  assert.equal(g.isDone(), false);
  g.markDone();
  assert.equal(g.isDone(), true);
});

test("terminal classification: first success announces, later ones are quiet/ignored", () => {
  assert.equal(classifyTerminal("success", false), "announce");
  assert.equal(classifyTerminal("success", true), "ignore");
  assert.equal(classifyTerminal("already_handled", false), "quiet");
  assert.equal(classifyTerminal("already_handled", true), "ignore");
  assert.equal(classifyTerminal("waiting", false), "ignore");
});
```

- [ ] **Step 2: 验证 RED**

Run: `node --test web/lib/__tests__/poll-guard.test.mjs`
Expected: FAIL — `Cannot find module '../poll-guard.ts'`（模块不存在，不是拼写错误）。

- [ ] **Step 3: 最小实现** `web/lib/poll-guard.ts`

```ts
/** 加账号轮询的代次与一次性交付守卫。纯函数，不依赖 React/DOM，便于 node:test 直接跑。 */
export interface PollGuard {
  begin(): number;
  invalidate(): number;
  isStale(generation: number): boolean;
  markDone(): void;
  isDone(): boolean;
}

export function createPollGuard(): PollGuard {
  let generation = 0;
  let done = false;
  return {
    begin: () => ++generation,
    invalidate: () => ++generation,
    isStale: (g) => g !== generation,
    markDone: () => {
      done = true;
    },
    isDone: () => done,
  };
}

export type TerminalAction = "ignore" | "announce" | "quiet";

/**
 * 决定一次 poll 终态该怎么处理：
 * - announce：正常成功，弹 toast、关弹窗、刷列表；
 * - quiet  ：服务端说这份结果已被本客户端消费过（重复轮询），静默收尾，不弹 toast；
 * - ignore ：本轮已完成 / 非终态，什么都不做。
 */
export function classifyTerminal(
  status: string,
  delivered: boolean,
): TerminalAction {
  if (delivered || status === "waiting") return "ignore";
  if (status === "already_handled") return "quiet";
  if (status === "success") return "announce";
  return "ignore";
}
```

- [ ] **Step 4: 验证 GREEN**

Run: `node --test web/lib/__tests__/poll-guard.test.mjs` → 6 PASS；`cd web && npx --no-install tsc --noEmit` → 0 error。

- [ ] **Step 5: 提交** `git add web/lib/poll-guard.ts web/lib/__tests__/poll-guard.test.mjs && git commit -m "test(web): 加账号轮询守卫的失败用例与纯逻辑（A 内核）"`

---

### Task 2: 前端 AddAccountDialog 接入 guard（A 落地）

**Files:**

- Modify: `web/components/common/accounts/AddAccountDialog.tsx`（refs、stopPoll、start、tick、success 分支）
- Modify: `web/lib/api.ts:295-305`（`accountApi.poll` 增 `client` 参数）
- Test: `web/lib/__tests__/poll-guard.test.mjs`（补「组件确实接入」静态断言）

**Interfaces:**

- Consumes: `createPollGuard`, `classifyTerminal`（Task 1）。
- Produces: `accountApi.poll(..., client?: string)` 把 `client` 作为 `client` query 上送；`AddAccountDialog` 用 `sessionStorage` 里的 `wb.add-account.client`（随机 uuid，缺失则生成）作为客户端实例 id。

- [ ] **Step 1: 补失败测试**（追加到同一 `.test.mjs`）

```js
import { readFileSync } from "node:fs";
test("AddAccountDialog actually uses the guard", () => {
  const src = readFileSync(
    new URL(
      "../../components/common/accounts/AddAccountDialog.tsx",
      import.meta.url,
    ),
    "utf8",
  );
  assert.match(src, /createPollGuard/);
  assert.match(src, /classifyTerminal/);
});
test("accountApi.poll forwards the client id", () => {
  const src = readFileSync(
    new URL("../../lib/api.ts", import.meta.url),
    "utf8",
  );
  assert.match(src, /client:\s*client/);
});
```

- [ ] **Step 2: 验证 RED**

Run: `node --test web/lib/__tests__/poll-guard.test.mjs`
Expected: 2 FAIL（组件里没有 `createPollGuard`；api.ts 没有 `client` 透传）。前 6 条仍 PASS。

- [ ] **Step 3: 最小实现**

1. `web/lib/api.ts` 的 `poll` 签名改为 `(state, realm?, region?, upstreamId?, client?: string)`，`get` 的 params 加 `client: client ?? undefined`。
2. `AddAccountDialog.tsx`：
   - import：`import {createPollGuard, classifyTerminal} from '@/lib/poll-guard';`
   - 模块级 helper：
     ```ts
     function clientId(): string {
       if (typeof window === "undefined") return "";
       const KEY = "wb.add-account.client";
       let id = window.sessionStorage.getItem(KEY) || "";
       if (!id) {
         id =
           globalThis.crypto?.randomUUID?.() ??
           `${Date.now()}-${Math.random()}`;
         window.sessionStorage.setItem(KEY, id);
       }
       return id;
     }
     ```
   - refs 区加 `const guardRef = useRef(createPollGuard());`、`const deliveredRef = useRef(false);`
   - `stopPoll` 开头加 `guardRef.current.invalidate();`（保留原 `clearInterval` + `removeEventListener` + `pollingRef=false`）。
   - `start`：`deliveredRef.current = false;`；`const generation = guardRef.current.begin();` 放在 `stopPoll()` 之后、`await accountApi.start` 之前；`await` 回来后第一行 `if (guardRef.current.isStale(generation)) return;`。
   - `tick` 开头加 `if (guardRef.current.isStale(generation) || deliveredRef.current) return;`（在 `document.hidden` 之前）。
   - `await accountApi.poll(..., clientId())` 回来后加 `if (guardRef.current.isStale(generation) || deliveredRef.current) return;`。
   - success 分支改为：
     ```ts
     const action = classifyTerminal(res.status, deliveredRef.current);
     if (action === "ignore") return;
     deliveredRef.current = true;
     stopPoll();
     setPhase("success");
     const accountName = res.nickname || res.uid || "";
     setMessage(
       res.updated
         ? t("addAccount.successUpdated", { name: accountName })
         : t("addAccount.success", { name: accountName }),
     );
     if (action === "announce") {
       notify.ok(
         t("addAccount.success", { name: accountName }),
         res.realm === "global"
           ? t("addAccount.successGlobal")
           : res.updated
             ? t("addAccount.successToken")
             : t("addAccount.successCheckin"),
       );
     }
     window.dispatchEvent(new Event("workbuddy-manager:accounts-changed"));
     onSuccess?.();
     window.setTimeout(() => onOpenChange(false), 1600);
     ```
   - `res.status === 'expired' | 'invalid' | 'realm_mismatch'` 分支不动。
3. `already_handled` 由 `classifyTerminal` 归为 `'quiet'`，无需在 `tick` 里另列分支（它走上面 success 附近的终态处理，但 `status` 不等于原来的 `success`）——**把 `if (res.status === 'success')` 的条件放宽为 `if (res.status === 'success' || res.status === 'already_handled')`**，其余分支保持。

- [ ] **Step 4: 验证 GREEN**

Run: `node --test web/lib/__tests__/poll-guard.test.mjs` → 8 PASS；`cd web && npx --no-install tsc --noEmit` → 0 error。

- [ ] **Step 5: 提交** `git commit -am "fix(web): 加账号轮询按代次失效，成功只弹一次（A）"`

---

### Task 3: 服务端并发去重 RED 用例（B + C 的测试）

**Files:**

- Create: `server/tests/test_login_dedup.py`
- Modify: `server/tests/test_login_server_poll.py`（`LoginPollTestCase.setUp/tearDown` 清新增状态；`_run_poller` 给 `auth_poll` 补 `client` 默认值兼容）

**Interfaces:**

- Consumes: `LoginPollTestCase`、`tencent`、`accounts` 模块级状态。
- Produces: 钉住三个尚不存在的契约：`_login_locks`（按 state 的锁）、`_login_delivered: dict[str, set[str]]`（state → 已交付的 client 集合）、`auth_poll(..., client: str | None = None)`、poll 入口 prune。

- [ ] **Step 1: 写失败测试**（完整文件）

```python
"""加账号「授权成功」重复弹出 / 同一 state 并发落盘两次的回归钉子。

根因（2026-11-05，amendolagoodfellow@gmail.com）：
  1. 前端 realm/region/proxy 连续变化让 start() 重入，旧的 setInterval 泄漏；
  2. 服务端后台轮询与前端 HTTP 轮询并发，且 ready→供给→落盘 全程无锁，
     同一个号被落盘两次（checkin_logs 里两条 source='add'）；
  3. 后台任务把 success 存进 _login_polls 且只在下次 auth_start 才 prune，
     每个泄漏 tick 都拿到 status='success' → notify.ok 无限弹。

本文件只钉服务端契约（B/C）；前端由 web/lib/__tests__/poll-guard.test.mjs 覆盖。
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import tencent
from server.tests.test_login_server_poll import LoginPollTestCase, _READY, _USER


class ConcurrentPollTest(LoginPollTestCase):
    def _patch_pipeline(self, write):
        return (
            mock.patch.object(tencent, 'poll_login',
                              mock.AsyncMock(return_value=dict(_READY))),
            mock.patch.object(tencent, 'checkin',
                              mock.AsyncMock(return_value=(0, 'ok'))),
            mock.patch.object(tencent, 'write_auth_file', side_effect=write),
            mock.patch.object(self.accounts.reload, 'request_reload_or_restart'),
        )

    def test_concurrent_polls_land_only_one_write(self) -> None:
        """两个 auth_poll 并发读同一个 ready state，只能落盘一次。"""
        writes: list[int] = []

        async def write(account, auth_dir=None):  # noqa: ANN001
            await asyncio.sleep(0.01)          # 拉长竞态窗口
            writes.append(1)
            return ('workbuddy-u-bg.json', False)

        p1, p2, p3, p4 = self._patch_pipeline(write)
        tencent._state_cache['s-race'] = (time.time(), 'cn')

        async def scenario():
            with p1, p2, p3, p4:
                return await asyncio.wait_for(asyncio.gather(
                    self.accounts.auth_poll(state='s-race', realm='cn',
                                            region=None, upstream_id=None, user=_USER),
                    self.accounts.auth_poll(state='s-race', realm='cn',
                                            region=None, upstream_id=None, user=_USER),
                ), timeout=5)

        outs = asyncio.run(scenario())
        self.assertEqual(len(writes), 1,
                         f'并发轮询把同一个号落盘了 {len(writes)} 次（现场是 2 次）')
        self.assertTrue(all(o['status'] == 'success' for o in outs))

    def test_same_client_gets_success_once_then_already_handled(self) -> None:
        self.accounts._login_polls['s-dup'] = {
            'status': 'success', 'uid': 'u-bg', 'nickname': '背景账号',
            'realm': 'cn', 'updated': False, 'file': 'workbuddy-u-bg.json',
            'region_note': '', '_at': time.time(),
        }
        c = self._client()
        first = c.get('/api/auth/poll', params={'state': 's-dup', 'client': 'win-A'})
        second = c.get('/api/auth/poll', params={'state': 's-dup', 'client': 'win-A'})
        self.assertEqual(first.json()['status'], 'success')
        self.assertEqual(second.json()['status'], 'already_handled',
                         '同一客户端重复读终态 —— 泄漏的 tick 会一直弹「授权成功」')

    def test_second_window_still_gets_one_success(self) -> None:
        """多窗口契约：另一个客户端首次读仍要拿到 success（不能全局只发一次）。"""
        self.accounts._login_polls['s-two'] = {
            'status': 'success', 'uid': 'u-bg', 'nickname': '背景账号',
            'realm': 'cn', 'updated': False, 'file': 'workbuddy-u-bg.json',
            'region_note': '', '_at': time.time(),
        }
        c = self._client()
        a = c.get('/api/auth/poll', params={'state': 's-two', 'client': 'win-A'})
        b = c.get('/api/auth/poll', params={'state': 's-two', 'client': 'win-B'})
        self.assertEqual(a.json()['status'], 'success')
        self.assertEqual(b.json()['status'], 'success',
                         '第二个窗口被全局去重挡掉了 —— 会永远停在「等待授权完成」')

    def test_expired_terminal_is_pruned_on_poll(self) -> None:
        self.accounts._login_polls['s-old'] = {
            'status': 'success', '_at': time.time() - self.accounts._LOGIN_RESULT_TTL - 1,
        }
        self._client().get('/api/auth/poll', params={'state': 's-other'})
        self.assertNotIn('s-old', self.accounts._login_polls)

    def test_lock_released_after_save_failure(self) -> None:
        """落盘失败后锁必须释放，否则用户点「重试」会死锁。"""
        calls = {'n': 0}

        async def write(account, auth_dir=None):  # noqa: ANN001
            calls['n'] += 1
            raise PermissionError('permission denied')

        p1, p2, p3, p4 = self._patch_pipeline(write)
        tencent._state_cache['s-fail'] = (time.time(), 'cn')

        async def scenario():
            with p1, p2, p3, p4:
                for _ in range(2):
                    try:
                        await self.accounts.auth_poll(
                            state='s-fail', realm='cn', region=None,
                            upstream_id=None, user=_USER)
                    except Exception:
                        pass

        asyncio.run(asyncio.wait_for(scenario(), timeout=5))
        self.assertEqual(calls['n'], 2, '第二次被锁卡住了（死锁或落盘未重试）')

    def test_cancel_during_inflight_poll_does_not_deadlock(self) -> None:
        """auth_start 取消旧后台任务时，持有锁的任务仍能正常结束/被取消。"""
        entered = asyncio.Event()

        async def slow_poll(state, realm=None):
            entered.set()
            await asyncio.sleep(10)
            return {'status': 'waiting'}

        async def scenario():
            with mock.patch.object(tencent, 'poll_login', slow_poll), \
                 mock.patch.object(tencent, 'checkin',
                                   mock.AsyncMock(return_value=(0, 'ok'))):
                task = asyncio.create_task(self.accounts.auth_poll(
                    state='s-cancel', realm='cn', region=None,
                    upstream_id=None, user=_USER))
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2)
                except (asyncio.CancelledError, TimeoutError):
                    pass

        asyncio.run(asyncio.wait_for(scenario(), timeout=6))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 验证 RED**

Run: `python3 -m unittest server.tests.test_login_dedup -v`
Expected: 三条**因功能缺失**失败——并发用例 `writes == 2`、同客户端重复读仍是 `success`、过期终态仍在；`test_second_window_still_gets_one_success` 与两条锁用例当前应通过（作为不回归护栏）。若它们 RED，先修正用例写法，不得假装 RED。

- [ ] **Step 3: 最小实现**（`server/routers/accounts.py`）

1. 模块级（`:409` 附近）加：
   ```python
   _login_locks: dict[str, asyncio.Lock] = {}
   _login_delivered: dict[str, set[str]] = {}   # state → 已交付的 client 集合

   def _login_lock(state: str) -> asyncio.Lock:
       return _login_locks.setdefault(state, asyncio.Lock())
   ```
2. `auth_poll` 签名（`:763-769`）加 `client: str | None = Query(None)`。
3. `auth_poll` 入口（`:775` 之后）加 `_prune_login_polls()`。
4. 终态缓存短路处（`:783-786`）改为：
   ```python
   if cached and cached.get('status') not in (None, 'waiting'):
       key = client or ''            # 无 client 的旧调用方：不参与去重（保持兼容）
       seen = _login_delivered.setdefault(state, set())
       if key and key in seen:
           return {'status': 'already_handled'}
       if key:
           seen.add(key)
       return {k: v for k, v in cached.items() if not k.startswith('_')}
   ```
5. 把 `:790` 起「`if region:` → `group = _group(...)` → ... → `return out`（`:881`）」整段包进 `async with _login_lock(state):`。缓存短路与 prune 留在锁外。
6. `_prune_login_polls()`（`:440`）：每个被回收的 `st` 同步 `_login_delivered.pop(st, None)`、`_login_locks.pop(st, None)`；`_login_ready` 过期分支同样。
7. `_save_and_finish`（`:1082-1083`）成功后 `_login_delivered.pop(state, None)`、`_login_locks.pop(state, None)`。
8. `LoginPollTestCase.setUp/tearDown` 补 `_login_delivered.clear()` / `_login_locks.clear()`。

- [ ] **Step 4: 验证 GREEN**

Run:

```
python3 -m unittest server.tests.test_login_dedup -v
python3 -m unittest server.tests.test_login_server_poll server.tests.test_global_login_activation server.tests.test_realm -v
cd web && npx --no-install tsc --noEmit
```

Expected: 全部 PASS / 0 error（`test_terminal_result_does_not_ask_tencent_again` 不带 client，按兼容分支仍只短路一次，应仍绿）。

- [ ] **Step 5: 提交** `git commit -am "fix(server): 按 state 加锁 + 终态按 client 只交付一次 + poll 时 prune（B/C）"`

---

### Task 4: 全量回归与重建部署

**Files:**

- Regenerate: `web/out/**`
- Verify: `docker compose up -d --build`

- [ ] **Step 1: 服务端全量回归**

Run: `python3 -m unittest discover -s server/tests -t . 2>&1 | tail -40`
Expected: 结果与 d372f42 提交说明一致（17 个预先存在的失败：command palette / settings tabs / request binding / api tokens）。**逐一点名**，不得把新失败藏进「预先存在」。任何 `test_login_*` / `test_realm` / `test_global_login_activation` 失败即为回归，回 Task 3。

- [ ] **Step 2: 前端类型检查 + 构建产物重建**

Run:

```
cd web && npx --no-install tsc --noEmit
cd web && NEXT_OUTPUT_EXPORT=1 npx --no-install next build
```

Expected: 0 error；生成 `web/out/index.html`。产物由源重建，不手改。

- [ ] **Step 3: 重建并部署**

Run: `docker compose up -d --build`
Expected: 容器 `healthy`。

- [ ] **Step 4: 运行验证**

Run:

```
docker ps --format '{{.Names}} {{.Status}}' | grep workbuddy-manager
curl -fsS http://127.0.0.1:7864/api/healthz
docker logs workbuddy-manager --tail 20 2>&1 | grep -iE "error|traceback" || echo "无错误"
```

Expected: healthy、healthz 正常、无新 traceback；运行 uid 仍非 root。

- [ ] **Step 5: 现场复现验证**（人工，记录结果）

打开国际版添加账号弹窗 → 选地区 → 完成授权。观察：成功 toast **只出现一次**；`docker logs workbuddy-manager --since 2m | grep auth/poll` 在成功后不再持续刷；`data/manager.db` 里该 uid 的 `checkin_logs` 只有一条 `source='add'`。再开第二个窗口看同一结果仍各自收尾。

- [ ] **Step 6: 提交**

`git add -A && git commit -m "chore: 重建 web/out 并全量回归（重复授权成功提示 A+B+C）"`
按红线**不提 PR**；告知分支名与改动摘要。

---

## Self-Review

1. **Spec coverage**：A（Task 1/2）、B（Task 3 锁）、C（Task 3 delivered + prune）各有任务；Task 4 Step 5 覆盖用户可观察结果「不再一直弹」；Global Constraints 的「多窗口契约」由 `client` 去重键 + `test_second_window_still_gets_one_success` 落实。
2. **Placeholder scan**：无 TBD / TODO / 「适当处理」；每个代码步都有完整代码块。`classifyTerminal` 的 `'quiet'` 在 Task 2 Step 3 明确落地为「静默收尾、不弹 toast」。
3. **Type consistency**：`createPollGuard` 的五个方法在 Task 1 定义、Task 2 使用一致；`classifyTerminal(status, delivered)` 两处一致；`_login_locks` / `_login_delivered` / `client` 在 Task 3 定义、清理、路由签名一致；`accountApi.poll` 的 `client` 参数在 api.ts 与 AddAccountDialog 一致。
4. **Review Focus**：5 条分别由 Task 1/2（重开/连续重入）、Task 3（多窗口、失败不死锁、取消不死锁、TTL）、Task 4（现场）覆盖；每条都在对应任务里有具名用例，无空项。
