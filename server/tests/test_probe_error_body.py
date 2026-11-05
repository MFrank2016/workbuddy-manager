"""上游错误响应解析的回归测试：嵌套业务码 + HTTP 状态码兜底。

背景（2026-11-05）：国际版新号没完成地区注册时，腾讯对 chat 返回

    HTTP 429
    {"error":{"data":{"code":14017,"msg":"The trial version is not yet activated..."}}}

`_parse_error_body` 只读响应体**顶层** `code`，读不到就回退成 HTTP 状态码 429，
面板于是显示光秃秃的「上游返回 code=429」——和真正的 6004 限流长得一模一样，
用户被误导成「账号被限流了」，其实是「试用未激活、需要完成地区注册」。

上游 Go 侧（internal/upstream/client.go:683）专门注明「14017 常带 429 状态码，
必须先于 status==429 判定，否则误归 soft_rate」。本端也要把真实业务码读出来。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import tencent


class ParseErrorBodyTest(unittest.TestCase):
    """嵌套 error.data.code 必须优先于 HTTP 状态码。"""

    def test_nested_trial_not_activated_over_http_429(self) -> None:
        """14017（试用未激活）不能只显示成 429 —— 这是本文件要钉住的主问题。"""
        body = (
            '{"error":{"data":{"code":14017,'
            '"msg":"The trial version is not yet activated. Please log out of your '
            "current account and log in again to activate it immediately and start "
            'your free trial.","requestId":"59eedd0c"}}}'
        )
        code, msg = tencent._parse_error_body(body, 429)
        self.assertEqual(code, 14017, "嵌套 error.data.code=14017 没被读出来")
        self.assertIn(
            "trial version is not yet activated",
            msg,
            "必须保留上游原文，用户才能看出是试用未激活而不是限流",
        )
        # 端到端口径：显示文案与分类都要跟着真实业务码走
        shown = tencent._explain_code(code, msg)
        self.assertIn("code=14017", shown)
        self.assertIn("试用未激活", shown, "14017 应给出「试用未激活」的可读提示")
        self.assertEqual(
            tencent.classify_probe_result(False, shown)[0],
            "error",
            "14017 不是限流（rate），不能误导用户去等量重置",
        )

    def test_nested_generic_code(self) -> None:
        """其它嵌套码（如 6004）也优先于状态码，且不能丢原文。"""
        body = '{"error":{"data":{"code":6004,"msg":"usage exceeds frequency limit"}}}'
        code, msg = tencent._parse_error_body(body, 429)
        self.assertEqual(code, 6004)
        self.assertEqual(msg, "usage exceeds frequency limit")

    def test_top_level_envelope_still_wins(self) -> None:
        """既有 {code,msg} 顶层信封行为不变（老接口不能回归）。"""
        code, msg = tencent._parse_error_body(
            '{"code":11102,"msg":"model missing"}', 400
        )
        self.assertEqual((code, msg), (11102, "model missing"))

    def test_error_message_only_wrapper(self) -> None:
        """只有 {"error":{"message":...}}、无业务码时，回退状态码但保留文案。"""
        code, msg = tencent._parse_error_body('{"error":{"message":"oops"}}', 503)
        self.assertEqual(code, 503)
        self.assertIn("oops", msg)

    def test_plain_text_falls_back_to_status(self) -> None:
        code, msg = tencent._parse_error_body("upstream exploded", 502)
        self.assertEqual(code, 502)
        self.assertEqual(msg, "upstream exploded")

    def test_invalid_json_falls_back_to_status(self) -> None:
        code, _msg = tencent._parse_error_body("{not json", 429)
        self.assertEqual(code, 429)


if __name__ == "__main__":
    unittest.main()
