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


def _terminal(state: str) -> dict:
    return {
        "status": "success",
        "uid": "u-bg",
        "nickname": "背景账号",
        "realm": "cn",
        "updated": False,
        "file": "workbuddy-u-bg.json",
        "region_note": "",
        "_at": time.time(),
    }


class ConcurrentPollTest(LoginPollTestCase):
    def _patch_pipeline(self, write):
        return (
            mock.patch.object(
                tencent, "poll_login", mock.AsyncMock(return_value=dict(_READY))
            ),
            mock.patch.object(
                tencent, "checkin", mock.AsyncMock(return_value=(0, "ok"))
            ),
            mock.patch.object(tencent, "write_auth_file", side_effect=write),
            mock.patch.object(self.accounts.reload, "request_reload_or_restart"),
        )

    def test_concurrent_polls_land_only_one_write(self) -> None:
        """两个 auth_poll 并发读同一个 ready state，只能落盘一次。"""
        writes: list[int] = []

        # write_auth_file 是同步调用（见 _save_and_finish），补丁函数也必须同步；
        # 用 async def 会让 unpack 拿到 coroutine 而报 TypeError。竞态窗口由
        # poll_login 的 await 提供：两次 auth_poll 会并发穿过 ready→供给→落盘。
        def write(account, auth_dir=None):  # noqa: ANN001
            writes.append(1)
            return ("workbuddy-u-bg.json", False)

        p1, p2, p3, p4 = self._patch_pipeline(write)
        tencent._state_cache["s-race"] = (time.time(), "cn")

        async def scenario():
            with p1, p2, p3, p4:
                return await asyncio.wait_for(
                    asyncio.gather(
                        self.accounts.auth_poll(
                            state="s-race",
                            realm="cn",
                            region=None,
                            upstream_id=None,
                            user=_USER,
                        ),
                        self.accounts.auth_poll(
                            state="s-race",
                            realm="cn",
                            region=None,
                            upstream_id=None,
                            user=_USER,
                        ),
                    ),
                    timeout=5,
                )

        outs = asyncio.run(scenario())
        self.assertEqual(
            len(writes), 1, f"并发轮询把同一个号落盘了 {len(writes)} 次（现场是 2 次）"
        )
        self.assertTrue(all(o["status"] == "success" for o in outs))

    def test_same_client_gets_success_once_then_already_handled(self) -> None:
        self.accounts._login_polls["s-dup"] = _terminal("s-dup")
        c = self._client()
        first = c.get("/api/auth/poll", params={"state": "s-dup", "client": "win-A"})
        second = c.get("/api/auth/poll", params={"state": "s-dup", "client": "win-A"})
        self.assertEqual(first.json()["status"], "success")
        self.assertEqual(
            second.json()["status"],
            "already_handled",
            "同一客户端重复读终态 —— 泄漏的 tick 会一直弹「授权成功」",
        )

    def test_already_handled_keeps_account_fields(self) -> None:
        """already_handled 仍要带账号字段：前端 quiet 收尾要拼文案，
        只回一个 status 会显示「账号「」授权成功」（Task 2 的 ruling）。"""
        self.accounts._login_polls["s-fields"] = _terminal("s-fields")
        c = self._client()
        c.get("/api/auth/poll", params={"state": "s-fields", "client": "win-A"})
        quiet = c.get("/api/auth/poll", params={"state": "s-fields", "client": "win-A"})
        body = quiet.json()
        self.assertEqual(body["status"], "already_handled")
        self.assertEqual(body.get("nickname"), "背景账号")
        self.assertEqual(body.get("uid"), "u-bg")
        self.assertEqual(body.get("realm"), "cn")

    def test_second_window_still_gets_one_success(self) -> None:
        """多窗口契约：另一个客户端首次读仍要拿到 success（不能全局只发一次）。"""
        self.accounts._login_polls["s-two"] = _terminal("s-two")
        c = self._client()
        a = c.get("/api/auth/poll", params={"state": "s-two", "client": "win-A"})
        b = c.get("/api/auth/poll", params={"state": "s-two", "client": "win-B"})
        self.assertEqual(a.json()["status"], "success")
        self.assertEqual(
            b.json()["status"],
            "success",
            "第二个窗口被全局去重挡掉了 —— 会永远停在「等待授权完成」",
        )

    def test_expired_terminal_is_pruned_on_poll(self) -> None:
        self.accounts._login_polls["s-old"] = {
            "status": "success",
            "_at": time.time() - self.accounts._LOGIN_RESULT_TTL - 1,
        }
        self._client().get("/api/auth/poll", params={"state": "s-other"})
        self.assertNotIn("s-old", self.accounts._login_polls)

    def test_lock_released_after_save_failure(self) -> None:
        """落盘失败后锁必须释放，否则用户点「重试」会死锁。"""
        calls = {"n": 0}

        def write(account, auth_dir=None):  # noqa: ANN001
            calls["n"] += 1
            raise PermissionError("permission denied")

        p1, p2, p3, p4 = self._patch_pipeline(write)
        tencent._state_cache["s-fail"] = (time.time(), "cn")

        async def scenario():
            with p1, p2, p3, p4:
                for _ in range(2):
                    try:
                        await self.accounts.auth_poll(
                            state="s-fail",
                            realm="cn",
                            region=None,
                            upstream_id=None,
                            user=_USER,
                        )
                    except Exception:
                        pass

        asyncio.run(asyncio.wait_for(scenario(), timeout=5))
        self.assertEqual(calls["n"], 2, "第二次被锁卡住了（死锁或落盘未重试）")

    def test_cancel_during_inflight_poll_does_not_deadlock(self) -> None:
        """auth_start 取消旧后台任务时，持有锁的任务仍能正常结束/被取消。"""
        entered = asyncio.Event()

        async def slow_poll(state, realm=None):
            entered.set()
            await asyncio.sleep(10)
            return {"status": "waiting"}

        async def scenario():
            with (
                mock.patch.object(tencent, "poll_login", slow_poll),
                mock.patch.object(
                    tencent, "checkin", mock.AsyncMock(return_value=(0, "ok"))
                ),
            ):
                task = asyncio.create_task(
                    self.accounts.auth_poll(
                        state="s-cancel",
                        realm="cn",
                        region=None,
                        upstream_id=None,
                        user=_USER,
                    )
                )
                await asyncio.wait_for(entered.wait(), timeout=2)
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2)
                except (asyncio.CancelledError, TimeoutError):
                    pass

        asyncio.run(asyncio.wait_for(scenario(), timeout=6))


if __name__ == "__main__":
    unittest.main()


class ReviewFindingsTest(LoginPollTestCase):
    """终审发现的回归钉子：只降级 success、重试幂等、记账可回收、
    不带 client 的直呼不参与去重、_login_completed 路径也按 client 去重。"""

    def _client_get(self, state, client=None):
        params = {'state': state}
        if client is not None:
            params['client'] = client
        return self._client().get('/api/auth/poll', params=params).json()

    # #1 非 success 终态不能被降级成 already_handled（否则失败的登录会显示授权成功）
    def test_non_success_terminal_is_never_downgraded(self) -> None:
        for status in ('expired', 'invalid', 'realm_mismatch'):
            with self.subTest(status=status):
                self.accounts._login_polls.clear()
                self.accounts._login_delivered.clear()
                self.accounts._login_polls[f's-{status}'] = {
                    'status': status, '_at': time.time(),
                }
                first = self._client_get(f's-{status}', 'win-A')
                second = self._client_get(f's-{status}', 'win-A')
                self.assertEqual(first['status'], status)
                self.assertEqual(second['status'], status,
                                 f'{status} 被当成 success 降级了 —— 会误报「授权成功」')

    # #4 直呼 auth_poll（不带 client）不该参与去重，Query 哨兵也不能混进 seen
    def test_direct_call_without_client_is_not_deduped(self) -> None:
        self.accounts._login_polls['s-direct'] = _terminal('s-direct')
        with mock.patch.object(tencent, 'poll_login',
                               mock.AsyncMock(return_value={'status': 'waiting'})):
            a = asyncio.run(self.accounts.auth_poll(
                state='s-direct', realm='cn', region=None,
                upstream_id=None, user=_USER))
            b = asyncio.run(self.accounts.auth_poll(
                state='s-direct', realm='cn', region=None,
                upstream_id=None, user=_USER))
        self.assertEqual(a['status'], 'success')
        self.assertEqual(b['status'], 'success',
                         '不带 client 的旧调用方被误去重了')
        seen = self.accounts._login_delivered.get('s-direct', set())
        self.assertTrue(all(isinstance(x, str) for x in seen),
                        f'seen 里混进了非字符串（Query 哨兵）: {seen!r}')

    # #7 _login_completed 路径也要按 client 去重（前端赢下竞态时）
    def test_completed_path_dedupes_per_client(self) -> None:
        self.accounts._login_completed['s-comp'] = _terminal('s-comp')
        a = self._client_get('s-comp', 'win-A')
        b = self._client_get('s-comp', 'win-A')
        c = self._client_get('s-comp', 'win-B')
        self.assertEqual(a['status'], 'success')
        self.assertEqual(b['status'], 'already_handled',
                         '_login_completed 路径重复交付了 success')
        self.assertEqual(c['status'], 'success', '另一个窗口被挡掉了')

    # #2 落盘失败后的重试：成功后只落盘一次、只回一次 success
    def test_retry_after_save_failure_is_idempotent(self) -> None:
        writes = {'n': 0}
        ready = dict(_READY)

        def write(account, auth_dir=None):  # noqa: ANN001
            writes['n'] += 1
            if writes['n'] == 1:
                raise PermissionError('permission denied')
            return ('workbuddy-u-bg.json', False)

        p1 = mock.patch.object(tencent, 'poll_login',
                               mock.AsyncMock(return_value=ready))
        p2 = mock.patch.object(tencent, 'checkin',
                               mock.AsyncMock(return_value=(0, 'ok')))
        p3 = mock.patch.object(tencent, 'write_auth_file', side_effect=write)
        p4 = mock.patch.object(self.accounts.reload, 'request_reload_or_restart')
        tencent._state_cache['s-retry'] = (time.time(), 'cn')

        async def scenario():
            with p1, p2, p3, p4:
                out = []
                for _ in range(3):
                    try:
                        out.append(await self.accounts.auth_poll(
                            state='s-retry', realm='cn', region=None,
                            upstream_id=None, user=_USER))
                    except Exception:
                        out.append('FAILED')
                return out

        outs = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
        self.assertEqual(writes['n'], 2,
                         f'重试把同一个号又落盘了多次: {writes["n"]}')
        # 直呼 auth_poll 不带 client，按约定不参与去重（#4），所以两次都回
        # success；关键是**第三次没有再落盘**，且备忘已清（终审 #2）。
        self.assertEqual([o if o == 'FAILED' else o['status'] for o in outs],
                         ['FAILED', 'success', 'success'], f'重试路径异常: {outs}')
        self.assertNotIn('s-retry', self.accounts._login_ready,
                         '成功后没清掉 _login_ready 备忘')

    # #3 所有新增记账表都要能按 TTL 回收（含 waiting-only 的锁）
    def test_all_login_bookkeeping_is_reclaimed(self) -> None:
        self.accounts._login_locks['s-w'] = asyncio.Lock()
        self.accounts._login_completed['s-c'] = _terminal('s-c')
        self.accounts._login_delivered['s-d'] = {'win-A'}
        # 模拟这些 state 已经过期
        old = time.time() - self.accounts._LOGIN_RESULT_TTL - 1
        self.accounts._login_touch['s-w'] = old
        self.accounts._login_touch['s-c'] = old
        self.accounts._login_touch['s-d'] = old
        self.accounts._prune_login_polls()
        for name in ('_login_locks', '_login_completed', '_login_delivered', '_login_touch'):
            self.assertNotIn(
                {'_login_locks': 's-w', '_login_completed': 's-c',
                 '_login_delivered': 's-d', '_login_touch': 's-w'}[name],
                getattr(self.accounts, name),
                f'{name} 没被回收 —— 长期运行会无限增长')

    # #3 重新发码取消旧轮询时，三张新表也要一起收掉
    def test_cancel_other_polls_cleans_new_tables(self) -> None:
        self.accounts._login_owner['old'] = 'admin|'
        self.accounts._login_tasks['old'] = mock.MagicMock(done=lambda: False)
        self.accounts._login_locks['old'] = asyncio.Lock()
        self.accounts._login_delivered['old'] = {'win-A'}
        self.accounts._login_completed['old'] = _terminal('old')
        self.accounts._login_touch['old'] = time.time()
        self.accounts._cancel_other_login_polls('new', 'admin|')
        for name in ('_login_locks', '_login_delivered', '_login_completed', '_login_touch'):
            self.assertNotIn('old', getattr(self.accounts, name),
                             f'{name} 在收掉旧轮询时没清 —— 会一直占位')
