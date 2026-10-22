"""设置页「模型路由」的七个管理端点（迁移 Todo 6，契约 §2）。

为什么这些端点值得单独钉一遍：路由表决定**下游模型名打到哪个上游模型**，改错一行
就是整条链路静默打到错误的模型上；而它的写入口还带着两个容易被顺手放开的边界：

  · **只对会话管理员开放**。改路由等于改「请求去哪」，与改上游分组同级；泄露的
    admin scope API Token 不该能重写它（`require_session_admin`，不是 `require_admin`）。
  · **错误码必须能区分可修复的原因**：非法值 / 分组不存在或停用 → 400（改请求就能
    成功）；未知 id → 404；`(public_id, realm, group)` 重复 → 409（要改的是那条已有
    的行）。全都压成 400 的话，界面只能显示「保存失败」。

另外两条不变量：

  · **总开关默认关**，开启后网关才走路由表，关闭后逐字回到 golden 行为（旧
    `model_map` / 原名透传）——升级窗口里谁也没要求换行为。
  · **每次写入一条审计**，且不含任何凭据（这里本来也没有凭据，但 detail 只记
    字段值，不记请求体原文）。

本文件只覆盖管理面；解析链与租户隔离在 `test_model_routes_resolve.py` /
`test_gateway_model_routes.py`，不在这里重复。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security, upstreamsvc  # noqa: E402
from server.routers import gateway as G  # noqa: E402

BASE = '/api/settings/model-routes'


class _ApiCase(unittest.TestCase):
    """独立临时库 + 一个 admin / 一个 viewer 的会话。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'routes-admin.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [
                {'username': 'admin', 'role': 'admin',
                 'pwd_hash': security.make_hash('admin-pw')},
                {'username': 'guest', 'role': 'viewer',
                 'pwd_hash': security.make_hash('guest-pw')},
            ],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()
        db.execute('DELETE FROM model_routes')
        db.execute('DELETE FROM upstreams')
        db.execute("DELETE FROM settings WHERE key IN ('model_routes_enabled', 'model_map')")

    def tearDown(self) -> None:
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()

    # ── 工具 ─────────────────────────────────────────────
    @contextmanager
    def _as(self, username: str, password: str):
        r = self.c.post('/api/login', json={'username': username, 'password': password})
        self.assertEqual(r.status_code, 200, r.text)
        self.c.cookies.update(dict(r.cookies))
        try:
            yield self.c
        finally:
            self.c.cookies.clear()

    def _mint_admin_token(self) -> str:
        with self._as('admin', 'admin-pw') as c:
            r = c.post('/api/tokens', json={'name': 'ci', 'scope': 'admin'})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['token']

    def _group(self, name: str = 'group-1', enabled: bool = True) -> int:
        created = upstreamsvc.create_upstream(
            name=name, base_url='http://127.0.0.1:9999', api_key='k')
        if not enabled:
            upstreamsvc.update_upstream(created['id'], {'enabled': False})
        return int(created['id'])

    def _rows(self) -> list[dict]:
        return [dict(r) for r in db.query('SELECT * FROM model_routes ORDER BY id')]

    def _audit_since(self, last_id: int) -> list[dict]:
        return [dict(r) for r in db.query(
            'SELECT * FROM audit_logs WHERE id > ? ORDER BY id', (last_id,))]

    def _last_audit_id(self) -> int:
        return int(db.query_one(
            'SELECT COALESCE(MAX(id), 0) AS n FROM audit_logs')['n'])


class CrudChainTest(_ApiCase):
    """POST → GET → PATCH → enabled → DELETE 全链路。"""

    def test_full_chain(self) -> None:
        gid = self._group()
        with self._as('admin', 'admin-pw') as c:
            created = c.post(BASE, json={
                'public_id': 'fast', 'target_model': 'deepseek-v4.1-flash',
                'realm': 'cn', 'upstream_group_id': gid,
                'keep_original': False, 'credits_override': 1.5,
            })
            self.assertEqual(created.status_code, 200, created.text)
            row = created.json()
            self.assertEqual(row['public_id'], 'fast')
            self.assertEqual(row['target_model'], 'deepseek-v4.1-flash')
            self.assertEqual(row['realm'], 'cn')
            self.assertEqual(row['upstream_group_id'], gid)
            self.assertIs(row['keep_original'], False)
            self.assertIs(row['enabled'], True)
            self.assertEqual(row['credits_override'], 1.5)
            self.assertIsInstance(row['id'], int)
            # 不得暴露账号绑定 / priority（契约只允许这几个字段）
            for forbidden in ('priority', 'account', 'accounts', 'account_id'):
                self.assertNotIn(forbidden, row)

            listed = c.get(BASE)
            self.assertEqual(listed.status_code, 200, listed.text)
            items = listed.json()['items']
            self.assertEqual([i['id'] for i in items], [row['id']])

            patched = c.patch(f'{BASE}/{row["id"]}',
                              json={'target_model': 'glm-5.2', 'realm': ''})
            self.assertEqual(patched.status_code, 200, patched.text)
            self.assertEqual(patched.json()['target_model'], 'glm-5.2')
            self.assertEqual(patched.json()['realm'], '')

            disabled = c.post(f'{BASE}/{row["id"]}/enabled', json={'enabled': False})
            self.assertEqual(disabled.status_code, 200, disabled.text)
            self.assertIs(disabled.json()['enabled'], False)

            deleted = c.delete(f'{BASE}/{row["id"]}')
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertEqual(c.get(BASE).json()['items'], [])

    def test_created_row_has_no_priority_column(self) -> None:
        """表里本来就没有 priority —— 接口也不许凭空造一个。"""
        with self._as('admin', 'admin-pw') as c:
            c.post(BASE, json={'public_id': 'a', 'target_model': 'b'})
        self.assertEqual(self._rows()[0]['public_id'], 'a')
        self.assertNotIn('priority', self._rows()[0])


class ReadPermissionTest(_ApiCase):
    def test_viewer_can_read_list_and_config(self) -> None:
        with self._as('guest', 'guest-pw') as c:
            self.assertEqual(c.get(BASE).status_code, 200)
            r = c.get(f'{BASE}/config')
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIs(r.json()['enabled'], False)

    def test_anonymous_is_rejected(self) -> None:
        self.assertEqual(self.c.get(BASE).status_code, 401)
        self.assertEqual(self.c.get(f'{BASE}/config').status_code, 401)


class WritePermissionTest(_ApiCase):
    """所有写端点：viewer 403、API Token 403（即使 scope=admin）。"""

    def test_viewer_cannot_write_anything(self) -> None:
        gid = self._group()
        with self._as('admin', 'admin-pw') as c:
            created = c.post(BASE, json={'public_id': 'a', 'target_model': 'b',
                                         'upstream_group_id': gid}).json()
        rid = created['id']
        with self._as('guest', 'guest-pw') as c:
            calls = [
                ('post', BASE, {'public_id': 'x', 'target_model': 'y'}),
                ('patch', f'{BASE}/{rid}', {'target_model': 'z'}),
                ('post', f'{BASE}/{rid}/enabled', {'enabled': False}),
                ('post', f'{BASE}/config', {'enabled': True}),
            ]
            for method, url, body in calls:
                with self.subTest(method=method, url=url):
                    r = getattr(c, method)(url, json=body)
                    self.assertEqual(r.status_code, 403, r.text)
            self.assertEqual(c.delete(f'{BASE}/{rid}').status_code, 403)
        self.assertEqual(len(self._rows()), 1, '被拒的写请求不能改库')
        self.assertIs(db.get_setting('model_routes_enabled', False), False,
                      'viewer 不能打开总开关')

    def test_api_token_cannot_write_anything(self) -> None:
        token = self._mint_admin_token()
        headers = {'Authorization': f'Bearer {token}'}
        calls = [
            ('post', BASE, {'public_id': 'x', 'target_model': 'y'}),
            ('post', f'{BASE}/config', {'enabled': True}),
        ]
        for method, url, body in calls:
            with self.subTest(url=url):
                r = getattr(self.c, method)(url, json=body, headers=headers)
                self.assertEqual(r.status_code, 403, r.text)
                self.assertIn('API Token', r.json().get('detail', ''))
        self.assertEqual(self._rows(), [])
        self.assertIs(db.get_setting('model_routes_enabled', False), False)


class ValidationTest(_ApiCase):
    """400 / 404 / 409 各归其位。"""

    def test_invalid_realm_is_400(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            r = c.post(BASE, json={'public_id': 'a', 'target_model': 'b', 'realm': 'us'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._rows(), [])

    def test_missing_public_id_or_target_is_400(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            for body in ({'target_model': 'b'}, {'public_id': 'a'},
                         {'public_id': '  ', 'target_model': 'b'},
                         {'public_id': 'a', 'target_model': '*'}):
                with self.subTest(body=body):
                    self.assertEqual(c.post(BASE, json=body).status_code, 400)

    def test_duplicate_key_is_409(self) -> None:
        gid = self._group()
        body = {'public_id': 'dup', 'target_model': 'b', 'realm': 'cn',
                'upstream_group_id': gid}
        with self._as('admin', 'admin-pw') as c:
            self.assertEqual(c.post(BASE, json=body).status_code, 200)
            r = c.post(BASE, json={**body, 'target_model': 'other'})
            self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(len(self._rows()), 1)

    def test_patch_into_an_existing_key_is_409(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            c.post(BASE, json={'public_id': 'a', 'target_model': 't1'})
            second = c.post(BASE, json={'public_id': 'b', 'target_model': 't2'}).json()
            r = c.patch(f'{BASE}/{second["id"]}', json={'public_id': 'a'})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(sorted(x['public_id'] for x in self._rows()), ['a', 'b'])

    def test_unknown_id_is_404(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            self.assertEqual(c.patch(f'{BASE}/999', json={'target_model': 'x'}).status_code,
                             404)
            self.assertEqual(c.delete(f'{BASE}/999').status_code, 404)
            self.assertEqual(
                c.post(f'{BASE}/999/enabled', json={'enabled': False}).status_code, 404)

    def test_unknown_group_is_400(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            r = c.post(BASE, json={'public_id': 'a', 'target_model': 'b',
                                   'upstream_group_id': 12345})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._rows(), [])

    def test_disabled_group_is_400(self) -> None:
        gid = self._group(enabled=False)
        with self._as('admin', 'admin-pw') as c:
            r = c.post(BASE, json={'public_id': 'a', 'target_model': 'b',
                                   'upstream_group_id': gid})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._rows(), [])

    def test_patch_onto_a_disabled_group_is_400(self) -> None:
        gid = self._group(enabled=False)
        with self._as('admin', 'admin-pw') as c:
            created = c.post(BASE, json={'public_id': 'a', 'target_model': 'b'}).json()
            r = c.patch(f'{BASE}/{created["id"]}', json={'upstream_group_id': gid})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNone(self._rows()[0]['upstream_group_id'])

    def test_enabled_body_must_be_boolean(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            created = c.post(BASE, json={'public_id': 'a', 'target_model': 'b'}).json()
            r = c.post(f'{BASE}/{created["id"]}/enabled', json={'enabled': 'yes'})
        self.assertEqual(r.status_code, 400, r.text)


class MasterSwitchTest(_ApiCase):
    """默认关 → 开启后路由生效 → 关闭回到 golden 行为。"""

    def test_default_off_enable_effective_disable_golden(self) -> None:
        self.assertIs(db.get_setting('model_routes_enabled', False), False,
                      '总开关默认必须是关（契约 §5）')
        db.execute(
            'INSERT INTO model_routes(public_id, target_model, realm, upstream_group_id, '
            'credits_override, keep_original, enabled, created_at, updated_at) '
            "VALUES('alias', 'route-target', '', NULL, NULL, 1, 1, 1, 1)")

        # 关：逐字回到旧路径（没有 model_map 时原样透传）
        self.assertEqual(G._map_model('alias', None), 'alias')

        with self._as('admin', 'admin-pw') as c:
            r = c.post(f'{BASE}/config', json={'enabled': True})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIs(r.json()['enabled'], True)
        self.assertEqual(G._map_model('alias', None), 'route-target',
                         '开启后网关必须走路由表')

        with self._as('admin', 'admin-pw') as c:
            self.assertIs(c.get(f'{BASE}/config').json()['enabled'], True)
            r = c.post(f'{BASE}/config', json={'enabled': False})
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(G._map_model('alias', None), 'alias',
                         '关闭后必须回到 golden 行为')

    def test_config_body_must_be_boolean(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            r = c.post(f'{BASE}/config', json={'enabled': 1})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIs(db.get_setting('model_routes_enabled', False), False)


class AuditTest(_ApiCase):
    """每次写入恰好一条审计，且不含敏感数据。"""

    def test_one_audit_row_per_write(self) -> None:
        gid = self._group()
        with self._as('admin', 'admin-pw') as c:
            last = self._last_audit_id()
            created = c.post(BASE, json={'public_id': 'a', 'target_model': 'b',
                                         'upstream_group_id': gid}).json()
            self.assertEqual(len(self._audit_since(last)), 1)

            last = self._last_audit_id()
            c.patch(f'{BASE}/{created["id"]}', json={'target_model': 'c'})
            self.assertEqual(len(self._audit_since(last)), 1)

            last = self._last_audit_id()
            c.post(f'{BASE}/{created["id"]}/enabled', json={'enabled': False})
            self.assertEqual(len(self._audit_since(last)), 1)

            last = self._last_audit_id()
            c.post(f'{BASE}/config', json={'enabled': True})
            self.assertEqual(len(self._audit_since(last)), 1)

            last = self._last_audit_id()
            c.delete(f'{BASE}/{created["id"]}')
            rows = self._audit_since(last)
        self.assertEqual(len(rows), 1, '每次写入应写且只写一条审计')
        detail = ' '.join(str(r['detail']) for r in rows).lower()
        for secret in ('password', 'token', 'secret', 'api_key'):
            self.assertNotIn(secret, detail, '审计不能带敏感数据')


if __name__ == '__main__':
    unittest.main()
