"""国际版登录：选了地区也必须完成「注册激活」，不能只提交地区就落盘。

背景（2026-11-05）：用户反馈「刚选了地区，marlynnorwell721 这个号好像也是
429」。排查发现上游对未激活的号返回 HTTP 429 + code=14017（trial not activated）。
manager 的登录流程在**带 region** 时只做 submit_region + claim_trial，
**没有显式调激活 GET**（`GET /auth/realms/copilot/overseas/user/register`）；
只有「没选地区」的分支才会碰 registration_status。

对照上游 Go 面板（internal/panel/login.go:255 →
upstream.GlobalCompleteRegistration）：GET register 查状态 → 需补地区则提交 →
**再 GET register 验证激活**。scripts/global_region.py 的 complete_flow 同样是
「提交地区后重新 register 验证」。

GET register 就是激活端点（上游脚本函数名 activate_region，注释写「注册激活」），
不是纯只读查询。所以 manager 必须在提交地区之后补一次激活调用并复核，
否则新号会卡在 14017，表现为「选了地区还是 429」。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import tencent
from server.tests.test_login_server_poll import LoginPollTestCase

_USER = {"username": "admin", "role": "admin"}
_READY_GLOBAL = {
    "status": "ready",
    "uid": "u-gl",
    "nickname": "new-global",
    "enterprise_id": "",
    "access_token": "AT",
    "refresh_token": "RT",
    "expires_at": 9999999999,
    "domain": "www.workbuddy.ai",
    "realm": "global",
}


class GlobalLoginActivationTest(LoginPollTestCase):
    """带地区登录国际版号：提交地区后必须显式激活并复核。"""

    def _run_poller(self, *, region="HK", submit_ok=True, activate_ok=True):
        submit = mock.AsyncMock(
            return_value=(submit_ok, "地区已提交" if submit_ok else "提交地区失败")
        )
        activate = mock.AsyncMock(
            return_value=(
                activate_ok,
                "地区注册已完成" if activate_ok else "尚未完成地区注册",
            )
        )
        with (
            mock.patch.object(
                tencent, "poll_login", mock.AsyncMock(return_value=dict(_READY_GLOBAL))
            ),
            mock.patch.object(tencent, "submit_region", submit),
            mock.patch.object(tencent, "registration_status", activate),
            mock.patch.object(
                tencent, "claim_trial", mock.AsyncMock(return_value=(True, "已领取"))
            ),
            mock.patch.object(self.accounts, "_LOGIN_POLL_SECONDS", 0.01),
            mock.patch.object(
                tencent, "checkin", mock.AsyncMock(return_value=(0, "ok"))
            ),
            mock.patch.object(
                tencent, "write_auth_file", return_value=("workbuddy-u-gl.json", False)
            ),
            mock.patch.object(self.accounts.reload, "request_reload_or_restart"),
        ):
            asyncio.run(
                self.accounts._poll_login_background(
                    "s-act", "global", region, None, _USER
                )
            )
        return submit, activate

    def test_region_login_activates_registration(self) -> None:
        """回归钉子：选了地区也要调激活 GET，否则新号聊天报 14017。"""
        submit, activate = self._run_poller(region="HK")
        self.assertTrue(submit.called, "地区仍应提交")
        self.assertTrue(
            activate.called,
            "提交地区后必须调 GET register 完成激活 —— 只 submit_region "
            "不够，新号会卡在 14017（上游 Go 的 GlobalCompleteRegistration "
            "在提交地区后会重新 register 验证）",
        )

    def test_activation_failure_surfaces_in_region_note(self) -> None:
        """激活复核失败时，落盘返回里要说清楚，不能假装成功。"""
        _submit, activate = self._run_poller(region="SG", activate_ok=False)
        self.assertTrue(activate.called)
        cached = self.accounts._login_polls.get("s-act") or {}
        note = str(cached.get("region_note", ""))
        self.assertIn(
            "激活", note, f"激活失败应体现在 region_note 供用户排查，实际={note!r}"
        )

    def test_submit_failure_skips_activation(self) -> None:
        """地区都没提交成功时不该白调激活（保持既有失败路径语义）。"""
        _submit, activate = self._run_poller(region="TH", submit_ok=False)
        self.assertFalse(
            activate.called, "submit_region 失败时应跳过激活，不该继续打上游"
        )


if __name__ == "__main__":
    unittest.main()
