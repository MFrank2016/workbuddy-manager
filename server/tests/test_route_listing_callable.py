"""清单与解析必须同一口径：**列出来的每个别名，用同一把密钥都调得通**。

## 为什么需要这一条

`/v1/models` 用 `key.realm` 挑路由行，而调用侧 `_map_model` 用
`db.realm_of_model(legacy or model)` 挑 —— 后者只看**模型名**（以及旧 `model_map`），
从不看路由行的 `realm` 列。于是一行

    public_id='alias', target_model='global:gpt-5.6-sol', realm='global'

对限定国际版的密钥：清单把它列出来（按 `key.realm` 选到了这一行），调用却按
`alias` 无前缀 → 判成国内版 → 找不到 `realm='global'` 的行 → 原样透传
`alias` → 版本检查 400 `realm_mismatch`。这正是 issue #46 的
「能选、一选就失败」，而 `modellisting.py` 的存在就是为了防它。

## 这里钉住的两件事

1. **写入口径**：`modelroutes.validate()` 拒绝「realm 与 public_id 前缀不符」与
   「目标模型与 public_id 版本不同」的行（`*` 兜底行按它自己的规则）。
2. **读出口径（纵深防御）**：清单用与解析**同一个**可达性判据
   （`modelroutes.reachable`）挑行，因此库里已存在的坏行也**永远不会被列出**。

矩阵覆盖：cn / global / 不限版本（`realm=''`）的密钥 × 绑定 / 未绑定分组 ×
`keep_original` 0 / 1。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc  # noqa: E402
from server.routers import gateway as G  # noqa: E402
from server.services import modelmap, modelroutes  # noqa: E402

ROUTE_INSERT = (
    'INSERT INTO model_routes(public_id, target_model, realm, upstream_group_id, '
    'credits_override, keep_original, enabled, created_at, updated_at) '
    'VALUES(?, ?, ?, ?, NULL, ?, 1, 1, 1)'
)

# 上游清单形状：国内版裸名 + 国际版带前缀
PAYLOAD = {'object': 'list', 'data': [
    {'id': 'glm-5.2'},
    {'id': 'deepseek-v4.1-flash'},
    {'id': 'global:deepseek-v4.1-flash'},
    {'id': 'global:gpt-5.6-sol'},
]}


def _key(realm: str = '', upstream_id: int | None = None,
         models: list[str] | None = None) -> dict:
    """一把能通过其它所有校验的密钥，只让版本 / 分组 / 白名单参与判定。"""
    return {'enabled': True, 'expires_at': 0, 'quota': 0, 'quota_credit': 0,
            'used_tokens': 0, 'used_credit': 0, 'ip_allowlist': [], 'max_ips': 0,
            'id': 1, 'realm': realm, 'models': list(models or []),
            'upstream_id': upstream_id}


def _route(public_id: str, target: str, realm: str = '',
           group: int | None = None, keep_original: int = 1) -> None:
    db.execute(ROUTE_INSERT, (public_id, target, realm, group, keep_original))


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'listing.db'
        db._conn = None
        db.connect()
        db.set_setting('model_routes_enabled', True)

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        finally:
            db._conn = None
            config.DB_PATH = self._orig
            self._tmp.cleanup()

    def _ids(self, key: dict) -> list[str]:
        out = G._scope_models(PAYLOAD, key)
        return [m['id'] for m in out['data']]

    def _assert_invariant(self, key: dict) -> list[str]:
        """不变量：清单里的每个 id，用同一把密钥调用都必须被放行。"""
        ids = self._ids(key)
        for mid in ids:
            mapped = G._map_model(mid, key)
            reason = keysvc.validate(key, '1.2.3.4', mid, mapped_model=mapped)
            self.assertIsNone(
                reason,
                f'清单给出的 {mid!r} 调用被拒（{reason}）——issue #46 的「能选、一选就失败」',
            )
        return ids


class CrossRealmRowIsNeverAdvertisedTest(_Case):
    """缺陷复现：行的 realm 列与 public_id 前缀不符。"""

    def test_global_key_does_not_see_a_prefixless_global_row(self) -> None:
        """`public_id='alias'` + `realm='global'` 对国际版密钥：不可达 → 不列出。"""
        _route('alias', 'global:gpt-5.6-sol', 'global')

        ids = self._assert_invariant(_key(realm='global'))

        self.assertNotIn('alias', ids, '列出了一个调用必然 400 的别名（issue #46）')
        self.assertEqual(ids, ['global:deepseek-v4.1-flash', 'global:gpt-5.6-sol'])

    def test_keep_original_zero_does_not_leave_the_bad_alias_alone(self) -> None:
        """`keep_original=0` 时别名是唯一候选——更不能把它列出来。"""
        _route('alias', 'global:gpt-5.6-sol', 'global', keep_original=0)

        ids = self._assert_invariant(_key(realm='global'))

        self.assertNotIn('alias', ids)
        self.assertIn('global:deepseek-v4.1-flash', ids)

    def test_cn_key_does_not_see_a_global_prefixed_cn_row(self) -> None:
        """镜像：`public_id='global:foo'` + `realm='cn'` 对国内版密钥不可达。"""
        _route('global:foo', 'glm-5.2', 'cn')

        ids = self._assert_invariant(_key(realm='cn'))

        self.assertNotIn('global:foo', ids)

    def test_unbound_key_does_not_see_either_bad_row(self) -> None:
        """未绑定密钥取两版并集，但坏行在两版里都不可达。"""
        _route('alias', 'global:gpt-5.6-sol', 'global')
        _route('global:foo', 'glm-5.2', 'cn')

        ids = self._assert_invariant(_key())

        self.assertNotIn('alias', ids)
        self.assertNotIn('global:foo', ids)


class AdvertisedMeansCallableTest(_Case):
    """矩阵：版本 × 绑定分组 × keep_original，合法别名都要「列出即可调」。"""

    LEGAL = [
        ('alias-cn', 'glm-5.2', 'cn'),
        ('global:alias', 'global:gpt-5.6-sol', 'global'),
    ]
    # 库里的坏行（直接 INSERT 绕过 validate，模拟历史脏数据）
    BAD = [
        ('alias', 'global:gpt-5.6-sol', 'global'),
        ('global:foo', 'glm-5.2', 'cn'),
    ]

    def _seed(self, keep_original: int) -> None:
        for public_id, target, realm in self.LEGAL + self.BAD:
            _route(public_id, target, realm, keep_original=keep_original)

    def test_every_listed_alias_resolves(self) -> None:
        for keep_original in (1, 0):
            for realm in ('', 'cn', 'global'):
                for group in (None, 1):
                    with self.subTest(keep_original=keep_original, realm=realm, group=group):
                        self.tearDown()
                        self.setUp()
                        self._seed(keep_original)
                        key = _key(realm=realm, upstream_id=group)
                        ids = self._assert_invariant(key)

                        # 合法别名按版本可见；坏行任何组合下都不可见
                        if realm in ('', 'cn'):
                            self.assertIn('alias-cn', ids)
                        if realm in ('', 'global'):
                            self.assertIn('global:alias', ids)
                        if realm == 'cn':
                            self.assertNotIn('global:alias', ids)
                        if realm == 'global':
                            self.assertNotIn('alias-cn', ids)
                        for bad, _, _ in self.BAD:
                            self.assertNotIn(bad, ids)

    def test_legal_alias_resolves_to_its_target(self) -> None:
        self._seed(1)

        self.assertEqual(G._map_model('alias-cn', _key(realm='cn')), 'glm-5.2')
        self.assertEqual(G._map_model('global:alias', _key(realm='global')),
                         'global:gpt-5.6-sol')

    def test_bound_group_row_is_visible_only_to_that_group(self) -> None:
        _route('alias-cn', 'glm-5.2', 'cn', group=2)

        self.assertNotIn('alias-cn', self._ids(_key(realm='cn', upstream_id=1)))
        self.assertIn('alias-cn', self._ids(_key(realm='cn', upstream_id=2)))


class LegacyUpgradeRowStillWorksTest(_Case):
    """升级窗口的回填行（realm='' + 目标带前缀）不能被这次修复误伤（issue #47）。

    解析的 realm 来自 `realm_of_model(legacy or model)` —— 旧 `model_map` 仍在
    参与判定，所以清单也必须用同一判据，否则「回填后开启路由表」会让原本能用的
    别名从清单里消失。
    """

    def setUp(self) -> None:
        super().setUp()
        db.set_setting('model_map', {'claude-fable-5': 'global:deepseek-v4.1-flash'})
        _route('claude-fable-5', 'global:deepseek-v4.1-flash', '')

    def test_global_key_lists_and_calls_it(self) -> None:
        key = _key(realm='global')

        ids = self._assert_invariant(key)

        self.assertIn('claude-fable-5', ids)
        self.assertEqual(G._map_model('claude-fable-5', key), 'global:deepseek-v4.1-flash')

    def test_unbound_key_lists_and_calls_it(self) -> None:
        key = _key()

        self.assertIn('claude-fable-5', self._assert_invariant(key))
        self.assertEqual(G._map_model('claude-fable-5', key), 'global:deepseek-v4.1-flash')

    def test_cn_key_does_not_get_the_global_alias(self) -> None:
        ids = self._assert_invariant(_key(realm='cn'))

        self.assertNotIn('claude-fable-5', ids, '国内版密钥的清单里出现了国际版别名')


class ValidateRejectsInconsistentRealmTest(unittest.TestCase):
    """写入口径：这种行只可能产出 `realm_mismatch`，存进去之前就拒绝。"""

    def _reject(self, row: dict) -> str:
        with self.assertRaises(ValueError) as caught:
            modelroutes.validate([row])
        return str(caught.exception)

    def test_realm_disagreeing_with_public_id_prefix_is_rejected(self) -> None:
        msg = self._reject({'public_id': 'alias', 'target_model': 'global:gpt-5.6-sol',
                            'realm': 'global'})
        self.assertIn('版本', msg)

        msg = self._reject({'public_id': 'global:foo', 'target_model': 'glm-5.2',
                            'realm': 'cn'})
        self.assertIn('版本', msg)

    def test_target_in_another_realm_is_rejected(self) -> None:
        """显式 realm 与**目标模型**的版本不符 → 命中后立刻 400。"""
        msg = self._reject({'public_id': 'alias', 'target_model': 'global:x',
                            'realm': 'cn'})
        self.assertIn('版本', msg)

        msg = self._reject({'public_id': 'global:alias', 'target_model': 'glm-5.2',
                            'realm': 'global'})
        self.assertIn('版本', msg)

    def test_blank_realm_row_may_target_either_realm(self) -> None:
        """realm='' 的行对两版都适用（升级窗口的 `alias → global:x`，issue #47）。

        它只会被列给**目标版本**的密钥（清单侧按目标版本过滤），所以合法。
        """
        rows = modelroutes.validate([{'public_id': 'alias',
                                     'target_model': 'global:x', 'realm': ''}])

        self.assertEqual(rows[0]['realm'], '')

    def test_consistent_rows_are_accepted(self) -> None:
        rows = modelroutes.validate([
            {'public_id': 'alias', 'target_model': 'glm-5.2', 'realm': 'cn'},
            {'public_id': 'alias', 'target_model': 'glm-5.2', 'realm': ''},
            {'public_id': 'global:alias', 'target_model': 'global:x', 'realm': 'global'},
            {'public_id': 'global:alias', 'target_model': 'global:x', 'realm': ''},
        ])

        self.assertEqual([r['realm'] for r in rows], ['cn', '', 'global', ''])

    def test_star_rows_keep_working(self) -> None:
        """`*` 是兜底键、不是模型名：realm 由调用方的模型名决定，不由 `*` 决定。"""
        rows = modelroutes.validate([
            {'public_id': '*', 'target_model': 'glm-5.2', 'realm': ''},
            {'public_id': '*', 'target_model': 'global:x', 'realm': 'global'},
            {'public_id': '*', 'target_model': 'glm-5.2', 'realm': 'cn'},
        ])

        self.assertEqual(len(rows), 3)

    def test_star_row_whose_realm_cannot_match_its_target_is_rejected(self) -> None:
        """`*` + 专属 realm + 另一版本的目标 = 只可能命中后立刻 400。"""
        with self.assertRaises(ValueError):
            modelroutes.validate([{'public_id': '*', 'target_model': 'glm-5.2',
                                   'realm': 'global'}])
        with self.assertRaises(ValueError):
            modelroutes.validate([{'public_id': '*', 'target_model': 'global:x',
                                   'realm': 'cn'}])


class ReachableHelperTest(_Case):
    """`reachable` 与解析链同一判据（`*` 行按「键」处理）。"""

    def _alias(self, public_id: str, target: str, realm: str) -> modelroutes.Alias:
        return modelroutes.Alias(public_id=public_id, target_model=target, realm=realm,
                                 upstream_group_id=None, credits_override=None,
                                 keep_original=True)

    def test_star_row_is_reachable_by_its_own_realm(self) -> None:
        _route(modelmap.FALLBACK_KEY, 'global:x', 'global')
        star = self._alias(modelmap.FALLBACK_KEY, 'global:x', 'global')

        self.assertTrue(modelroutes.reachable(star, 'global'))
        self.assertFalse(modelroutes.reachable(star, 'cn'))

    def test_model_row_needs_its_name_realm(self) -> None:
        _route('alias', 'glm-5.2', 'cn')
        cn = self._alias('alias', 'glm-5.2', 'cn')

        self.assertTrue(modelroutes.reachable(cn, 'cn'))
        self.assertFalse(modelroutes.reachable(cn, 'global'))

    def test_row_that_loses_to_a_higher_precedence_one_is_not_reachable(self) -> None:
        """同一个 public_id 的空白 realm 行会被专属 realm 行盖住。"""
        _route('m', 'cn-target', 'cn')
        _route('m', 'blank-target', '')
        blank = self._alias('m', 'blank-target', '')

        self.assertFalse(modelroutes.reachable(blank, 'cn'), '该行不是解析会命中的那行')
        self.assertTrue(modelroutes.reachable(blank, 'global'))



if __name__ == '__main__':
    unittest.main()
