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
