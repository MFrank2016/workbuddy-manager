"""`model_routes` 的 6 级解析链、清单别名与保存校验（迁移契约 §2）。

解析链的**顺序本身就是契约**：每一步只问一个问题，问到了就停。唯一索引
`(public_id, realm, COALESCE(group,0))` 保证每一步**至多一行** —— 所以这里
既没有 `priority` 列，也没有「同优先级取哪条」的排序逻辑。顺序错一级，用户看到
的就是「配了路由却不生效」，而且只在特定 realm/分组的组合下复现。

本文件钉住：
  1. 6 级顺序逐级命中（含 `*` 的四个子级）；
  2. realm='' 的行对两个版本都适用；realm 专属行优先于它；
  3. 租户隔离：绑定组 2 的行对绑定组 1 的密钥**不可见**，未绑定密钥只吃 NULL 组；
  4. 禁用行跳过；
  5. 表为空 → 旧 `model_map`；表非空但没命中 → 原样透传（**不回退旧表**）；
  6. `aliases()` 的 `keep_original` 与 `credits_override`（仅展示）；
  7. `validate()` 拒绝重复与非法值；绕过它直接 INSERT 撞唯一索引。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.services import modelroutes  # noqa: E402

INSERT = (
    'INSERT INTO model_routes(public_id, target_model, realm, upstream_group_id, '
    'credits_override, keep_original, enabled, created_at, updated_at) '
    'VALUES(?, ?, ?, ?, ?, ?, ?, 1, 1)'
)

# 完整 fixture：一个 public_id 的 4 级 + `*` 的 4 个兜底子级。
# 目标名带级号，断言失败时一眼看出「跳到了第几级」。
ROWS = [
    ('m', 't1-realm-group', 'cn', 1, None, 1, 1),
    ('m', 't2-realm-null', 'cn', None, None, 1, 1),
    ('m', 't3-blank-realm-group', '', 1, None, 1, 1),
    ('m', 't4-blank-realm-null', '', None, None, 1, 1),
    ('*', 't5-star-realm-group', 'cn', 1, None, 1, 1),
    ('*', 't6-star-realm-null', 'cn', None, None, 1, 1),
    ('*', 't7-star-blank-group', '', 1, None, 1, 1),
    ('*', 't8-star-blank-null', '', None, None, 1, 1),
]


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'resolve.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        finally:
            db._conn = None
            config.DB_PATH = self._orig
            self._tmp.cleanup()

    def _seed(self, rows=ROWS) -> None:
        for row in rows:
            db.execute(INSERT, row)

    def _id_of(self, target: str) -> int:
        return int(db.query_one(
            'SELECT id FROM model_routes WHERE target_model = ?', (target,))['id'])

    def _disable(self, target: str) -> None:
        db.execute('UPDATE model_routes SET enabled = 0 WHERE id = ?', (self._id_of(target),))


class OrderedChainTest(_Case):
    """逐级禁用前一级 → 下一级接管，最后原样透传。顺序即契约。"""

    def test_six_levels_hit_in_contract_order(self) -> None:
        self._seed()
        expected = [
            't1-realm-group',        # 1. public + realm + group
            't2-realm-null',         # 2. public + realm + NULL
            't3-blank-realm-group',  # 3. public + '' + group
            't4-blank-realm-null',   # 4. public + '' + NULL
            't5-star-realm-group',   # 5a. * + realm + group
            't6-star-realm-null',    # 5b. * + realm + NULL
            't7-star-blank-group',   # 5c. * + '' + group
            't8-star-blank-null',    # 5d. * + '' + NULL
        ]

        for want in expected:
            with self.subTest(level=want):
                self.assertEqual(modelroutes.resolve('m', 'cn', 1), want)
                self._disable(want)

        # 6. 表非空、全部未命中 → 原样透传（**不回退旧 model_map**）
        self.assertEqual(modelroutes.resolve('m', 'cn', 1), 'm')

    def test_realm_specific_rows_win_over_blank_realm(self) -> None:
        self._seed()
        self._disable('t1-realm-group')
        self._disable('t2-realm-null')

        # realm 专属两级都没了 → 空 realm 的组行（第 3 级）
        self.assertEqual(modelroutes.resolve('m', 'cn', 1), 't3-blank-realm-group')

    def test_other_realm_skips_realm_specific_rows(self) -> None:
        self._seed()

        # global 没有专属行 → 第 3 级（public + '' + group），不是 cn 的行
        self.assertEqual(modelroutes.resolve('m', 'global', 1), 't3-blank-realm-group')

    def test_blank_realm_rows_serve_both_realms(self) -> None:
        self._seed([('m', 'both', '', None, None, 1, 1)])

        self.assertEqual(modelroutes.resolve('m', 'cn', None), 'both')
        self.assertEqual(modelroutes.resolve('m', 'global', None), 'both')

    def test_star_fallback_serves_unknown_models(self) -> None:
        self._seed()

        self.assertEqual(modelroutes.resolve('never-seen', 'cn', 1), 't5-star-realm-group')

    def test_exact_row_wins_over_star(self) -> None:
        self._seed()

        self.assertEqual(modelroutes.resolve('m', 'cn', 1), 't1-realm-group')


class TenantIsolationTest(_Case):
    def test_row_bound_to_another_group_is_invisible(self) -> None:
        self._seed()

        # 组 1 的行对组 2 的密钥不可见 → 落到 NULL 组行
        self.assertEqual(modelroutes.resolve('m', 'cn', 2), 't2-realm-null')
        self._disable('t2-realm-null')
        self.assertEqual(modelroutes.resolve('m', 'cn', 2), 't4-blank-realm-null')

    def test_group_only_row_does_not_leak_to_other_group(self) -> None:
        self._seed([('m', 'group1-only', 'cn', 1, None, 1, 1)])

        self.assertEqual(modelroutes.resolve('m', 'cn', 2), 'm')

    def test_unbound_key_only_matches_null_group_rows(self) -> None:
        self._seed()

        # 未绑定 = key_group None → 只有 NULL 组行参与（第 2 级）
        self.assertEqual(modelroutes.resolve('m', 'cn', None), 't2-realm-null')
        self._disable('t2-realm-null')
        self.assertEqual(modelroutes.resolve('m', 'cn', None), 't4-blank-realm-null')

    def test_unbound_key_cannot_use_group_bound_star_row(self) -> None:
        self._seed([('*', 'group1-star', 'cn', 1, None, 1, 1)])

        self.assertEqual(modelroutes.resolve('whatever', 'cn', None), 'whatever')


class DisabledTest(_Case):
    def test_disabled_row_is_skipped(self) -> None:
        self._seed([('m', 'off', 'cn', None, None, 1, 0),
                    ('m', 'on', '', None, None, 1, 1)])

        self.assertEqual(modelroutes.resolve('m', 'cn', None), 'on')

    def test_only_disabled_rows_means_passthrough(self) -> None:
        """表非空（只是全禁用）→ 透传；空表回退旧表是**升级窗口**专用语义。"""
        self._seed([('m', 'off', 'cn', None, None, 1, 0)])

        self.assertEqual(modelroutes.resolve('m', 'cn', None), 'm')

    def test_disabled_star_row_is_skipped(self) -> None:
        self._seed([('*', 'star-off', '', None, None, 1, 0)])

        self.assertEqual(modelroutes.resolve('unknown', 'cn', None), 'unknown')


class LegacyCompatibilityTest(_Case):
    def test_empty_table_falls_back_to_model_map(self) -> None:
        db.set_setting('model_map', {'m': 'legacy-target', '*': 'legacy-fallback'})

        self.assertEqual(modelroutes.resolve('m', 'cn', None), 'legacy-target')
        self.assertEqual(modelroutes.resolve('unknown', 'cn', None), 'legacy-fallback')

    def test_empty_table_without_model_map_passes_through(self) -> None:
        db.set_setting('model_map', {})

        self.assertEqual(modelroutes.resolve('m', 'cn', None), 'm')

    def test_non_empty_table_never_falls_back_to_model_map(self) -> None:
        """护栏：旧表只在**表为空**时参与，否则会与路由表打架。"""
        db.set_setting('model_map', {'m': 'legacy-target'})
        self._seed([('other', 'x', '', None, None, 1, 1)])

        self.assertEqual(modelroutes.resolve('m', 'cn', None), 'm')

    def test_none_and_empty_model_pass_through(self) -> None:
        self._seed()

        self.assertIsNone(modelroutes.resolve(None, 'cn', 1))
        self.assertEqual(modelroutes.resolve('', 'cn', 1), '')


class AliasesTest(_Case):
    def test_keep_original_one_keeps_upstream_id_plus_alias(self) -> None:
        self._seed([('alias', 'upstream-model', '', None, None, 1, 1)])

        alias = modelroutes.aliases(None)[0]

        self.assertEqual(alias.public_id, 'alias')
        self.assertEqual(alias.target_model, 'upstream-model')
        self.assertTrue(alias.keep_original)
        self.assertEqual(alias.ids, ('alias', 'upstream-model'))

    def test_keep_original_zero_keeps_only_public_id(self) -> None:
        self._seed([('alias', 'upstream-model', '', None, None, 0, 1)])

        alias = modelroutes.aliases(None)[0]

        self.assertFalse(alias.keep_original)
        self.assertEqual(alias.ids, ('alias',))

    def test_star_row_is_not_a_listing_alias(self) -> None:
        self._seed()

        ids = [a.public_id for a in modelroutes.aliases(1)]

        self.assertEqual(ids, ['m'])

    def test_disabled_and_foreign_group_rows_are_not_listed(self) -> None:
        self._seed([
            ('off', 'x', '', None, None, 1, 0),
            ('group2', 'y', '', 2, None, 1, 1),
            ('ok', 'z', '', 1, None, 1, 1),
        ])

        ids = [a.public_id for a in modelroutes.aliases(1)]

        self.assertEqual(ids, ['ok'])

    def test_rows_whose_realm_disagrees_with_their_name_are_not_listed(self) -> None:
        """历史脏行（realm 与 public_id 前缀不符）不可达 → 不列（issue #46）。

        合法的两种形状（`cn` 名字 + cn 行、`global:` 名字 + global 行）都列。
        """
        self._seed([
            ('cn-only', 'a', 'cn', None, None, 1, 1),
            ('global:only', 'global:b', 'global', None, None, 1, 1),
            ('bad-cn', 'c', 'global', None, None, 1, 1),
            ('global:bad', 'd', 'cn', None, None, 1, 1),
        ])

        self.assertEqual([a.public_id for a in modelroutes.aliases(None)],
                         ['cn-only', 'global:only'])

    def test_alias_uses_the_same_precedence_as_resolve(self) -> None:
        """同一 public_id 有多级行时，清单必须显示解析真正会命中的那条。"""
        self._seed([
            ('m', 'realm-group', 'cn', 1, None, 1, 1),
            ('m', 'blank-null', '', None, None, 1, 1),
        ])

        alias = modelroutes.aliases(1)[0]

        self.assertEqual(alias.target_model, 'realm-group')

    def test_credits_override_is_surfaced_for_display(self) -> None:
        self._seed([('alias', 'upstream-model', '', None, 1.5, 1, 1)])

        alias = modelroutes.aliases(None)[0]

        self.assertEqual(alias.credits_override, 1.5)
        self.assertEqual(alias.target_model, 'upstream-model')


class CreditsOverrideIsDisplayOnlyTest(_Case):
    def test_override_does_not_change_resolved_model(self) -> None:
        self._seed([('m', 'target', '', None, None, 1, 1)])
        plain = modelroutes.resolve('m', 'cn', None)
        db.execute('UPDATE model_routes SET credits_override = 9.5')

        self.assertEqual(modelroutes.resolve('m', 'cn', None), plain)

    def test_module_never_touches_quota_or_billing(self) -> None:
        """契约：override 只影响展示与排序，**绝不参与计费**。

        计费口径在 `keysvc.validate`（quota_credit/used_credit）与 billing 端点，
        解析模块一旦引用这些字段，就说明有人把展示值接进了扣费链路。
        """
        source = Path(modelroutes.__file__).read_text(encoding='utf-8')

        for forbidden in ('quota_credit', 'used_credit', 'billing'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class ValidateTest(_Case):
    def test_duplicate_payload_raises_value_error(self) -> None:
        payload = [
            {'public_id': 'm', 'target_model': 'a', 'realm': '', 'upstream_group_id': None},
            {'public_id': 'm', 'target_model': 'b', 'realm': '', 'upstream_group_id': None},
        ]

        with self.assertRaises(ValueError):
            modelroutes.validate(payload)

    def test_duplicate_null_and_zero_group_are_the_same_key(self) -> None:
        payload = [
            {'public_id': 'm', 'target_model': 'a', 'upstream_group_id': None},
            {'public_id': 'm', 'target_model': 'b', 'upstream_group_id': 0},
        ]

        with self.assertRaises(ValueError):
            modelroutes.validate(payload)

    def test_same_public_id_in_other_realm_or_group_is_allowed(self) -> None:
        payload = [
            {'public_id': 'm', 'target_model': 'a', 'realm': '', 'upstream_group_id': None},
            {'public_id': 'global:m', 'target_model': 'global:b', 'realm': 'global',
             'upstream_group_id': None},
            {'public_id': 'm', 'target_model': 'c', 'realm': '', 'upstream_group_id': 2},
        ]

        self.assertEqual(len(modelroutes.validate(payload)), 3)

    def test_same_name_in_other_realm_requires_a_matching_prefix(self) -> None:
        """`realm='global'` 只能配 `global:` 开头的 public_id（写入口径）。"""
        payload = [{'public_id': 'm', 'target_model': 'a', 'realm': 'global'}]

        with self.assertRaises(ValueError):
            modelroutes.validate(payload)

    def test_single_row_payload_is_accepted(self) -> None:
        rows = modelroutes.validate(
            {'public_id': 'm', 'target_model': 'a'})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['realm'], '')
        self.assertIsNone(rows[0]['upstream_group_id'])
        self.assertIsNone(rows[0]['credits_override'])
        self.assertEqual(rows[0]['keep_original'], 1)
        self.assertEqual(rows[0]['enabled'], 1)

    def test_invalid_values_raise_value_error(self) -> None:
        bad = [
            {'public_id': '', 'target_model': 'a'},
            {'public_id': 'm', 'target_model': ''},
            {'public_id': 'm', 'target_model': '*'},
            {'public_id': 'm', 'target_model': 'a', 'realm': 'us'},
            {'public_id': 'm', 'target_model': 'a', 'upstream_group_id': 0},
            {'public_id': 'm', 'target_model': 'a', 'credits_override': -1},
            {'public_id': 'm', 'target_model': 'a', 'keep_original': 'yes'},
            {'public_id': 'm', 'target_model': 'a', 'enabled': 'no'},
            'not-a-row',
        ]

        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValueError):
                modelroutes.validate([row])

    def test_direct_duplicate_insert_raises_integrity_error(self) -> None:
        """绕过 `validate()` 也必须被唯一索引拦住（最后一道防线）。"""
        db.execute(INSERT, ('m', 'a', '', None, None, 1, 1))

        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(INSERT, ('m', 'b', '', None, None, 1, 1))

    def test_direct_duplicate_group_insert_raises_integrity_error(self) -> None:
        db.execute(INSERT, ('m', 'a', 'cn', 3, None, 1, 1))

        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(INSERT, ('m', 'b', 'cn', 3, None, 1, 1))


if __name__ == '__main__':
    unittest.main()
