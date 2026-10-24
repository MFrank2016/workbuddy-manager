"""工具端点开关（迁移 Todo 29，契约 §4）。

钉住的性质：

  1. GET 任何登录用户可读；POST 只对会话管理员开放（viewer / API token 403）。
  2. 保存是**双写**：manager settings + 默认上游 core 配置（search/mcp）。
  3. core 写入失败 → 回滚 manager 的旧值并回 502（不能留下半开状态）。
  4. 存在非默认上游分组时 `core_writable=false`，POST 直接 400 并提示去该实例设置。
  5. 非法值（非布尔）400，且不改库。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, security, upstreamsvc  # noqa: E402

IP = '127.0.0.1'


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (config.DB_PATH, config.USERS_FILE, config.AUTH_DIR, config.STATIC_DIR)
        d = Path(self._tmp.name)
        config.DB_PATH = d / 'tools-settings.db'
        config.USERS_FILE = d / 'users.json'
        config.AUTH_DIR = d / 'auths'
        config.STATIC_DIR = d / 'no-static'
        db._conn = None
        db.connect()
        security.save_users({'secret': 'S' * 64, 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('admin-pw')},
            {'username': 'guest', 'role': 'viewer',
             'pwd_hash': security.make_hash('guest-pw')},
        ], 'api_keys': []})
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        (config.DB_PATH, config.USERS_FILE, config.AUTH_DIR,
         config.STATIC_DIR) = self._orig
        self._tmp.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        from server.main import app
        return TestClient(app, client=(IP, 50000))

    def _login(self, username: str, password: str):
        c = self._client()
        r = c.post('/api/login', json={'username': username, 'password': password})
        self.assertEqual(r.status_code, 200, r.text)
        c.cookies.update(dict(r.cookies))
        return c


class ToolsSettingsTest(_Base):
    def test_get_defaults(self) -> None:
        c = self._login('guest', 'guest-pw')
        r = c.get('/api/settings/tools')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIs(body['search_enabled'], False)
        self.assertIs(body['mcp_enabled'], False)
        self.assertIs(body['core_writable'], True)

    def test_viewer_post_is_403(self) -> None:
        c = self._login('guest', 'guest-pw')
        r = c.post('/api/settings/tools', json={'search_enabled': True})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIs(db.get_setting('tools_search_enabled', False), False)

    def test_api_token_post_is_403(self) -> None:
        admin = self._login('admin', 'admin-pw')
        r = admin.post('/api/tokens', json={'name': 'ci', 'scope': 'admin'})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()['token']
        c = self._client()
        r = c.post('/api/settings/tools', json={'search_enabled': True},
                   headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 403, r.text)

    def test_admin_save_writes_both_sides(self) -> None:
        c = self._login('admin', 'admin-pw')
        written = {'search': {'enabled': True}, 'mcp': {'enabled': False}}
        with mock.patch('server.services.wb2api.save_upstream_config',
                        return_value=written) as save, \
             mock.patch('server.services.wb2api.apply_config_hot',
                        new=mock.AsyncMock(return_value={'ok': True})) as hot:
            r = c.post('/api/settings/tools', json={'search_enabled': True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(db.get_setting('tools_search_enabled', False), True)
        save.assert_called_once_with({'search': {'enabled': True}})
        hot.assert_awaited_once_with(written)

    def test_core_failure_rolls_back_manager(self) -> None:
        c = self._login('admin', 'admin-pw')
        db.set_setting('tools_search_enabled', False)
        with mock.patch('server.services.wb2api.save_upstream_config',
                        side_effect=RuntimeError('disk full')):
            r = c.post('/api/settings/tools', json={'search_enabled': True})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertIs(db.get_setting('tools_search_enabled', False), False,
                      'manager setting must roll back when core write fails')

    def test_core_rejection_rolls_back_manager(self) -> None:
        c = self._login('admin', 'admin-pw')
        with mock.patch('server.services.wb2api.save_upstream_config', return_value={}), \
             mock.patch('server.services.wb2api.apply_config_hot',
                        new=mock.AsyncMock(return_value={
                            'ok': False, 'rejected': True, 'message': 'bad config'})):
            r = c.post('/api/settings/tools', json={'mcp_enabled': True})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertIs(db.get_setting('tools_mcp_enabled', False), False)

    def test_non_default_upstream_disables_core_write(self) -> None:
        upstreamsvc.create_upstream(name='grp', base_url='https://grp.example', api_key='sk')
        c = self._login('admin', 'admin-pw')
        r = c.get('/api/settings/tools')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()['core_writable'], False)
        self.assertEqual(r.json()['non_default_upstreams'], ['grp'])
        r = c.post('/api/settings/tools', json={'search_enabled': True})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('非默认上游分组', r.json()['detail'])
        self.assertIs(db.get_setting('tools_search_enabled', False), False)

    def test_non_boolean_is_400_and_unchanged(self) -> None:
        c = self._login('admin', 'admin-pw')
        r = c.post('/api/settings/tools', json={'search_enabled': 'true'})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIs(db.get_setting('tools_search_enabled', False), False)

    def test_audit_row_written(self) -> None:
        c = self._login('admin', 'admin-pw')
        with mock.patch('server.services.wb2api.save_upstream_config', return_value={}), \
             mock.patch('server.services.wb2api.apply_config_hot',
                        new=mock.AsyncMock(return_value={'ok': True})):
            r = c.post('/api/settings/tools', json={'mcp_enabled': True})
        self.assertEqual(r.status_code, 200, r.text)
        row = db.query_one("SELECT * FROM audit_logs WHERE action = 'update_tools_settings'")
        self.assertIsNotNone(row)
        self.assertNotIn('secret', str(row['detail']).lower())


if __name__ == '__main__':
    unittest.main()
