"""积分收支统计（`/api/stats/credits`）— 收入改走上游逐笔账本。

收入口径（方案 1）：上游 core 的 `/panel/api/credit_history` 逐笔 delta，
只累加 `delta > 0` 的行。这替换了旧的「余额快照差分」（task_logs.kind='credit'）
—— 后者会把多天收益合并记到查询日、且「先增后耗」时整笔漏记，导致每日收入
从 106 跳到 3319。逐笔账本是精确发放记录（+30 日活 / +100 签到 / +300~1000 活动）。

本文件钉住：
  1. 收入来自账本正 delta，不再查 task_logs；
  2. 只累加正 delta（消耗不进收入）；
  3. realm 按 uid 过滤；
  4. 上游不可用 → 降级为空并标记 unavailable，支出照常、不抛 5xx；
  5. 账本覆盖期外的日期 income=0、并带 covered_from / partial 标记；
  6. 区间内每天补齐（含无数据日）、days=1 时 24 桶小时下探、响应主体形状不变。
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import stats
from server.services import credit_ledger


def _day(offset: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() + offset * 86400))


def _e(day, hour, uid, delta):
    return {"day": day, "hour": hour, "uid": uid, "delta": delta}


class CreditsFlowLedgerTest(unittest.TestCase):
    """收入来自逐笔账本。"""

    def _run(
        self,
        *,
        entries=(),
        usable=True,
        usage_rows=(),
        days=7,
        realm=None,
        uids=None,
        covered_from=None,
    ):
        seen: list[tuple[str, tuple]] = []

        async def fake_fetch():
            return usable, list(entries)

        def fake_query(sql, args=()):
            seen.append((sql, tuple(args)))
            if "usage_daily" in sql:
                return list(usage_rows)
            if "usage_hourly" in sql:
                return []
            return []

        with (
            mock.patch.object(credit_ledger, "fetch_entries", fake_fetch),
            mock.patch.object(stats.db, "query", fake_query),
            mock.patch.object(stats, "_realm_uids", lambda r: uids),
        ):
            out = asyncio.run(
                stats.credits_flow(days=days, realm=realm, user={"role": "admin"})
            )
        return out, seen

    # ── 口径 ──────────────────────────────────────────────
    def test_income_uses_ledger_not_task_logs(self) -> None:
        """回归钉子：收入绝不能再查 task_logs 的余额差分。"""
        out, seen = self._run(entries=[_e(_day(0), 9, "u1", 30)])
        self.assertFalse(
            [s for s, _ in seen if "task_logs" in s],
            "收入已改走上游逐笔账本，不应再查 task_logs",
        )
        self.assertEqual(out["today"]["income"], 30)

    def test_only_positive_deltas_count_as_income(self) -> None:
        """消耗（负 delta）不能进收入；同一天多笔正 delta 累加。"""
        out, _ = self._run(
            entries=[
                _e(_day(0), 9, "u1", 30),
                _e(_day(0), 10, "u1", 100),
                _e(_day(0), 11, "u1", -59),  # 消耗
                _e(_day(0), 12, "u2", -3),  # 消耗
            ]
        )
        self.assertEqual(out["today"]["income"], 130)

    def test_daily_is_continuous_and_ends_today(self) -> None:
        out, _ = self._run(days=7)
        self.assertEqual(len(out["daily"]), 7)
        self.assertEqual(out["daily"][-1]["day"], _day(0))
        self.assertEqual(out["daily"][0]["day"], _day(-6))
        self.assertTrue(
            all(p["income"] == 0 and p["expense"] == 0 for p in out["daily"])
        )

    def test_net_is_income_minus_expense(self) -> None:
        out, _ = self._run(
            entries=[_e(_day(0), 9, "u1", 300)],
            usage_rows=[{"day": _day(0), "credit": 120}],
        )
        self.assertEqual(out["daily"][-1]["net"], 180)

    def test_today_week_total_sums(self) -> None:
        today, yday = _day(0), _day(-1)
        out, _ = self._run(
            entries=[
                _e(today, 9, "u1", 100),
                _e(yday, 9, "u1", 50),
            ],
            usage_rows=[{"day": today, "credit": 20}],
        )
        self.assertEqual(out["today"], {"income": 100, "expense": 20, "net": 80})
        self.assertEqual(out["week"]["income"], 150)
        self.assertEqual(out["total"]["income"], 150)

    def test_expense_query_targets_usage_daily(self) -> None:
        _, seen = self._run()
        usage = [s for s, _ in seen if "usage_daily" in s]
        self.assertTrue(usage)
        self.assertIn("SUM(credit)", usage[0])

    def test_days_clamped(self) -> None:
        out, _ = self._run(days=999999)
        self.assertEqual(out["days"], stats._DAYS_MAX)

    # ── realm ─────────────────────────────────────────────
    def test_realm_filters_ledger_by_uid(self) -> None:
        out, _ = self._run(
            realm="cn",
            uids={"cn-uid"},
            entries=[
                _e(_day(0), 9, "cn-uid", 100),
                _e(_day(0), 9, "global-uid", 30),
            ],
        )
        self.assertEqual(
            out["today"]["income"], 100, "realm=cn 时只累加 cn 账号的账本行"
        )

    def test_empty_realm_uid_set_yields_no_income(self) -> None:
        out, _ = self._run(realm="cn", uids=set(), entries=[_e(_day(0), 9, "x", 999)])
        self.assertEqual(out["today"]["income"], 0)

    def test_realm_filters_expense_by_column(self) -> None:
        _, seen = self._run(realm="global", uids={"uid-a"})
        usage = next((s, a) for s, a in seen if "usage_daily" in s)
        self.assertIn("realm = ?", usage[0])
        self.assertEqual(usage[1][-1], "global")

    # ── 上游不可用 / 覆盖期 ───────────────────────────────
    def test_upstream_unavailable_degrades_gracefully(self) -> None:
        """上游挂了：收入空、标 unavailable、支出照常、不抛异常。"""
        out, _ = self._run(usable=False, usage_rows=[{"day": _day(0), "credit": 42}])
        self.assertEqual(out["income_source"], "unavailable")
        self.assertEqual(out["today"]["income"], 0)
        self.assertEqual(out["today"]["expense"], 42)
        self.assertTrue(out["income_partial"])

    def test_covered_from_and_partial_markers(self) -> None:
        """账本只覆盖最近几天时，响应要标出最早日期与 partial。"""
        out, _ = self._run(entries=[_e(_day(0), 9, "u1", 30)], days=30)
        self.assertEqual(out["income_source"], "ledger")
        self.assertEqual(out["income_covered_from"], _day(0))
        self.assertTrue(
            out["income_partial"], "请求 30 天但账本只到今天 → 必须标 partial"
        )

    def test_partial_false_when_ledger_covers_range(self) -> None:
        """账本覆盖了整个请求区间时不该标 partial。"""
        entries = [_e(_day(-i), 9, "u1", 30) for i in range(7)]
        out, _ = self._run(entries=entries, days=7)
        self.assertFalse(out["income_partial"])
        self.assertEqual(out["income_covered_from"], _day(-6))

    def test_dates_outside_coverage_are_zero_not_fallback(self) -> None:
        """覆盖期外的日期 income=0，且绝不回退旧口径。"""
        out, seen = self._run(entries=[_e(_day(0), 9, "u1", 30)], days=7)
        self.assertFalse(
            [s for s, _ in seen if "task_logs" in s],
            "覆盖期外也不能回退到 task_logs 旧口径",
        )
        self.assertEqual(out["daily"][0]["income"], 0)
        self.assertEqual(out["daily"][-1]["income"], 30)


class CreditsFlowHourlyTest(unittest.TestCase):
    """days=1 时下探到小时（issue #285），响应形状不变。"""

    def _run(
        self,
        *,
        entries=(),
        usable=True,
        expense_hours=(),
        days=1,
        realm=None,
        uids=None,
    ):
        def fake_query(sql, args=()):
            if "usage_hourly" in sql:
                return [{"hour": h, "credit": v} for h, v in expense_hours]
            if "usage_daily" in sql:
                return [{"day": _day(0), "credit": sum(v for _, v in expense_hours)}]
            return []

        async def fake_fetch():
            return usable, list(entries)

        with (
            mock.patch.object(credit_ledger, "fetch_entries", fake_fetch),
            mock.patch.object(stats.db, "query", fake_query),
            mock.patch.object(stats, "_realm_uids", lambda r: uids),
        ):
            return asyncio.run(
                stats.credits_flow(days=days, realm=realm, user={"role": "admin"})
            )

    def test_single_day_returns_24_buckets(self) -> None:
        out = self._run(
            entries=[_e(_day(0), 9, "u1", 300)], expense_hours=[(9, 50.0), (18, 20.0)]
        )
        self.assertEqual(out["granularity"], "hour")
        self.assertEqual(len(out["hourly"]), 24)
        self.assertEqual([p["hour"] for p in out["hourly"]], list(range(24)))
        self.assertEqual(out["hourly"][9]["income"], 300.0)
        self.assertEqual(out["hourly"][9]["expense"], 50.0)
        self.assertEqual(out["hourly"][9]["net"], 250.0)
        self.assertEqual(out["hourly"][0]["income"], 0.0)

    def test_hourly_only_positive_deltas(self) -> None:
        out = self._run(
            entries=[
                _e(_day(0), 9, "u1", 30),
                _e(_day(0), 9, "u1", -5),
            ]
        )
        self.assertEqual(out["hourly"][9]["income"], 30.0)

    def test_multi_day_keeps_old_shape(self) -> None:
        out = self._run(days=7)
        self.assertEqual(out["granularity"], "day")
        self.assertNotIn("hourly", out)

    def test_hourly_filters_realm(self) -> None:
        out = self._run(
            realm="cn",
            uids={"cn-uid"},
            entries=[
                _e(_day(0), 9, "cn-uid", 100),
                _e(_day(0), 9, "g-uid", 30),
            ],
        )
        self.assertEqual(out["hourly"][9]["income"], 100.0)


if __name__ == "__main__":
    unittest.main()
