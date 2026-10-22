"""清单可达性收尾：shadow 行与跨版本 `*` 兜底（issue #46 的两类残余）。

## 两类缺陷

**A. shadow 行**：路由的 `public_id` 撞上**另一版本的真实上游模型名**，而 `realm`
留空。`validate()` 过去对空 realm 直接返回，于是
`{public_id: 'glm-5.2', target_model: 'global:gpt-5.6-sol', realm: ''}` 被接受：
国内版密钥的清单里 `glm-5.2` 仍是上游真名（`route_entries` 不会给已在清单里的
名字补别名条目），但调用时 `resolve` 把它改写成国际版目标 → 400 `realm_mismatch`。

**B. 跨版本 `*` 兜底**：`{public_id: '*', target_model: 'glm-5.2', realm: ''}`。
`'*'` 不进清单（它不是模型名），却会被 `resolve` 应用到**每一个**没精确命中的
模型上——国际版密钥请求任何国际版模型都被改写成国内版目标 → 全部 400。

## 两条防线（这里都钉住）

1. **写入口径**（`modelroutes.validate`）：非 `'*'` 行**无论 realm 是否留空**，
   `target_model` 的版本必须等于 `public_id` 的版本。跨版本的行存不进去。
2. **读出口径**（`modelroutes.resolve`）：选中的行若目标版本与请求版本不同，
   **跳过并继续查下一级**，绝不跨版本改写；什么都没命中就原样透传。这条同时
   覆盖 `'*'` 行（它仍然合法：解析会按请求版本作用域）与历史脏行。

清单与解析共用 `_pick`（`aliases` → `reachable` → `_pick`，`resolve` → `_pick`），
所以「列出来的每个 id 都调得通」是按构造成立的，不靠两处各写一遍。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc  # noqa: E402
from server.routers import gateway as G  # noqa: E402
from server.services import modelroutes  # noqa: E402

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

# 缺陷 A：public_id 是 cn 真名，目标却在 global
SHADOW = ('glm-5.2', 'global:gpt-5.6-sol')
# 缺陷 B：兜底键指向 cn 模型
STAR = ('*', 'glm-5.2')


def _key(realm: str = '', group: int | None = None,
         models: list[str] | None = None) -> dict:
    """一把只让版本 / 分组 / 白名单参与判定的密钥（其它校验一律满足）。"""
    return {'enabled': True, 'expires_at': 0, 'quota': 0, 'quota_credit': 0,
            'used_tokens': 0, 'used_credit': 0, 'ip_allowlist': [], 'max_ips': 0,
            'id': 1, 'realm': realm, 'models': list(models or []),
            'upstream_id': group}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'listing-realm.db'
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

    def _route(self, public_id: str, target: str, realm: str = '',
               group: int | None = None, keep_original: int = 1) -> None:
        db.execute(ROUTE_INSERT, (public_id, target, realm, group, keep_original))

    def _ids(self, key: dict) -> list[str]:
        return [m['id'] for m in G._scope_models(PAYLOAD, key)['data']]

    def _assert_invariant(self, key: dict) -> list[str]:
        """清单里的每个 id，用同一把密钥调用都不得被拒（更不能 realm_mismatch）。"""
        ids = self._ids(key)
        for mid in ids:
            reason = keysvc.validate(key, '1.2.3.4', mid,
                                     mapped_model=G._map_model(mid, key))
            self.assertIsNone(
                reason,
                f'清单给出的 {mid!r} 调用被拒（{getattr(reason, "code", reason)}）：'
                '这就是 issue #46 的「能选、一选就失败」',
            )
        return ids


class WritePathRejectsShadowRowTest(unittest.TestCase):
    """防线 1：跨版本的行在写入口就被拒绝（空 realm 不再是免检通道）。"""

    def _reject(self, row: dict) -> str:
        with self.assertRaises(ValueError) as caught:
            modelroutes.validate([row])
        return str(caught.exception)

    def test_blank_realm_shadow_row_is_rejected(self) -> None:
        msg = self._reject({'public_id': SHADOW[0], 'target_model': SHADOW[1],
                            'realm': ''})

        self.assertIn('target_model', msg)
        self.assertIn('public_id', msg)
        self.assertIn('版本', msg)

    def test_blank_realm_row_with_a_same_realm_target_is_still_accepted(self) -> None:
        """空 realm 依旧合法 —— 只要目标与 public_id 同版本（升级窗口的形状）。"""
        rows = modelroutes.validate([
            {'public_id': 'glm-5.2', 'target_model': 'deepseek-v4.1-flash', 'realm': ''},
            {'public_id': 'global:alias', 'target_model': 'global:x', 'realm': ''},
        ])

        self.assertEqual([r['realm'] for r in rows], ['', ''])

    def test_star_row_may_target_either_realm(self) -> None:
        """`'*'` 是兜底**键**：目标版本由请求决定，跨版本的目标仍然允许。"""
        rows = modelroutes.validate([
            {'public_id': '*', 'target_model': 'glm-5.2', 'realm': ''},
            {'public_id': '*', 'target_model': 'global:x', 'realm': '',
             'upstream_group_id': 1},
        ])

        self.assertEqual(len(rows), 2)


class ResolveRealmScopeTest(_Case):
    """防线 2：解析绝不跨版本改写；没命中就原样透传（`'*'` 也按请求版本作用域）。"""

    def test_star_row_does_not_rewrite_the_other_realm(self) -> None:
        self._route(*STAR, realm='')

        self.assertEqual(modelroutes.resolve('global:gpt-5.6-sol', 'global', None),
                         'global:gpt-5.6-sol', '跨版本改写：国际版请求被写成国内版模型')
        self.assertEqual(modelroutes.resolve('never-seen', 'global', None), 'never-seen')
        # 同版本仍然生效（`'*'` 不能被修成不可达）
        self.assertEqual(modelroutes.resolve('never-seen', 'cn', None), 'glm-5.2')

    def test_dirty_shadow_row_does_not_rewrite_across_realms(self) -> None:
        """绕过 validate 直接落库的历史脏行：解析跳过它，不改写。"""
        self._route(*SHADOW, realm='')

        # 解析的 realm 来自**模型名**（issue #47），所以两把密钥拿到的 realm 都是 cn；
        # 脏行目标在 global → 跳过 → 原样透传。
        self.assertEqual(G._map_model('glm-5.2', _key(realm='cn')), 'glm-5.2')
        self.assertEqual(G._map_model('glm-5.2', _key(realm='global')), 'glm-5.2')

    def test_cross_realm_row_falls_through_to_a_valid_lower_level(self) -> None:
        """第 1 级的行跨版本 → 跳过它，继续第 2/3 级，而不是直接透传。"""
        self._route('alias', 'global:gpt-5.6-sol', 'global', group=1)
        self._route('alias', 'glm-5.2', '', group=1)

        self.assertEqual(modelroutes.resolve('alias', 'cn', 1), 'glm-5.2')
        self.assertEqual(modelroutes.resolve('alias', 'global', 1), 'global:gpt-5.6-sol')


class ListingInvariantMatrixTest(_Case):
    """纯函数矩阵：cn / global / 不限版本 × 绑定 / 未绑定 × keep_original 0/1。"""

    def _seed(self, shadow: bool, star: bool, keep_original: int,
              group: int | None) -> None:
        if shadow:
            self._route(*SHADOW, realm='', group=group, keep_original=keep_original)
        if star:
            self._route(*STAR, realm='', group=group, keep_original=keep_original)

    def test_every_listed_id_is_callable(self) -> None:
        for shadow, star in ((True, False), (False, True), (True, True)):
            for keep_original in (1, 0):
                for realm in ('cn', 'global', ''):
                    for group in (None, 1):
                        with self.subTest(shadow=shadow, star=star,
                                          keep_original=keep_original,
                                          realm=realm, group=group):
                            self.tearDown()
                            self.setUp()
                            self._seed(shadow, star, keep_original, group)
                            key = _key(realm=realm, group=group)
                            ids = self._assert_invariant(key)

                            # 清单本身不能凭空消失：两版各至少留下一条真名
                            self.assertTrue(ids)
                            if realm == 'cn':
                                self.assertIn('glm-5.2', ids)
                            if realm == 'global':
                                self.assertIn('global:gpt-5.6-sol', ids)

    def test_star_row_still_rewrites_within_its_own_realm(self) -> None:
        """修复不能把 `'*'` 修成不可达：同版本时兜底照旧生效。"""
        self._seed(False, True, 1, None)

        self.assertEqual(G._map_model('never-seen', _key(realm='cn')), 'glm-5.2')
        self.assertEqual(G._map_model('global:never-seen', _key(realm='global')),
                         'global:never-seen')
