"""上游逐笔积分账本（credit_history）拉取与解析的回归测试。

`/api/stats/credits` 的收入口径从「余额快照差分」改为「上游逐笔正 delta」。
本模块负责向上游 core 的 `/panel/api/credit_history` 取账本并规范化：

  · 每条带 ISO 时间（含时区）→ 换算成本地 day / hour；
  · 保留 uid 与 delta（含负值），供 stats 层按 realm 过滤与判定覆盖期；
  · 上游不可达 / 非对象响应 / 解析失败 → (False, [])，由调用方降级，不抛。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import credit_ledger


def _entry(day, hour, uid, delta, tz="+08:00"):
    return {
        "time": f"{day}T{hour:02d}:12:34.123456{tz}",
        "uid": uid,
        "delta": delta,
        "before": 100,
        "after": 100 + delta,
    }


class FetchEntriesTest(unittest.TestCase):
    def _run(self, response=None, exc=None):
        async def fake_call(upstream, method, path, body=None, timeout=30.0):
            self.calls.append((method, path))
            if exc is not None:
                raise exc
            return response

        self.calls = []
        with (
            mock.patch.object(
                credit_ledger.panelapi,
                "resolve_upstream",
                return_value={"base_url": "http://core:7863"},
            ),
            mock.patch.object(credit_ledger.panelapi, "call", fake_call),
        ):
            return asyncio.run(credit_ledger.fetch_entries())

    def test_normalizes_positive_and_negative_entries(self) -> None:
        ok, entries = self._run(
            {
                "entries": [
                    _entry("2026-11-05", 9, "u1", 30),
                    _entry("2026-11-05", 9, "u1", -5),
                    _entry("2026-11-04", 23, "u2", 100),
                ]
            }
        )
        self.assertTrue(ok)
        self.assertEqual(len(entries), 3, "负 delta 也要保留（覆盖期要用全部条目判定）")
        self.assertEqual(
            entries[0], {"day": "2026-11-05", "hour": 9, "uid": "u1", "delta": 30}
        )
        self.assertEqual(entries[1]["delta"], -5)
        self.assertEqual(entries[2]["day"], "2026-11-04")
        self.assertEqual(entries[2]["hour"], 23)

    def test_requests_max_limit(self) -> None:
        """必须取满 1000 条上限，否则覆盖期会被人为截短。"""
        self._run({"entries": []})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "GET")
        self.assertIn("limit=1000", self.calls[0][1])

    def test_upstream_error_returns_unavailable(self) -> None:
        ok, entries = self._run(
            exc=credit_ledger.panelapi.PanelAPIError(502, "core down")
        )
        self.assertFalse(ok)
        self.assertEqual(entries, [])

    def test_non_object_response_returns_unavailable(self) -> None:
        ok, entries = self._run(response=["not", "a", "dict"])
        self.assertFalse(ok)
        self.assertEqual(entries, [])

    def test_missing_entries_returns_empty_but_available(self) -> None:
        ok, entries = self._run(response={"limit": 1000})
        self.assertTrue(ok)
        self.assertEqual(entries, [])

    def test_malformed_entry_is_skipped(self) -> None:
        ok, entries = self._run(
            {
                "entries": [
                    {"uid": "u1", "delta": 30},  # 缺 time
                    {"time": "not-a-time", "uid": "u1", "delta": 30},
                    {"time": "2026-11-05T09:00:00+08:00", "uid": "u1", "delta": "oops"},
                    _entry("2026-11-05", 9, "u1", 30),
                ]
            }
        )
        self.assertTrue(ok)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["delta"], 30)

    def test_utc_time_is_converted_to_local(self) -> None:
        """账本时间是带时区的 ISO；09:00Z 在本地(+08)应是 17 点。"""
        ok, entries = self._run(
            {
                "entries": [
                    {"time": "2026-11-05T09:00:00Z", "uid": "u1", "delta": 30},
                ]
            }
        )
        self.assertTrue(ok)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["hour"], 17)
        self.assertEqual(entries[0]["day"], "2026-11-05")


if __name__ == "__main__":
    unittest.main()
