"""core 面板 API 客户端（迁移 Phase 1 / Task 1）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, upstreamsvc  # noqa: E402
from server.services import panelapi  # noqa: E402


class _Client:
    def __init__(self, status: int = 200, body: object = None, text: str = '') -> None:
        self.status_code = status
        self._body = {'ok': True} if body is None else body
        self.text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, headers=None, json=None):
        self.last = (method, url, headers, json)
        return mock.Mock(status_code=self.status_code,
                         json=lambda: self._body,
                         text=self.text)


class PanelAPITest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        self._orig = (config.DB_PATH, config.USERS_FILE, config.AUTH_DIR)
        config.DB_PATH = d / 'p.db'
        config.USERS_FILE = d / 'users.json'
        config.AUTH_DIR = d / 'auths'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        (config.DB_PATH, config.USERS_FILE, config.AUTH_DIR) = self._orig

    def test_resolve_default(self) -> None:
        up = panelapi.resolve_upstream(None)
        self.assertTrue(up['is_default'])
        self.assertEqual(up['base_url'], config.WB2API_BASE)

    def test_resolve_non_default(self) -> None:
        up = upstreamsvc.create_upstream(name='g', base_url='https://g.example', api_key='sk-g')
        got = panelapi.resolve_upstream(int(up['id']))
        self.assertEqual(got['base_url'], 'https://g.example')
        self.assertEqual(got['api_key'], 'sk-g')

    def test_unknown_group_is_404(self) -> None:
        with self.assertRaises(panelapi.PanelAPIError) as ctx:
            panelapi.resolve_upstream(99999)
        self.assertEqual(ctx.exception.status, 404)

    def test_call_sends_bearer_and_parses_json(self) -> None:
        fake = _Client(200, {'ok': True})
        with mock.patch.object(config, 'http_client', lambda *a, **k: fake):
            out = panelapi.call_sync_for_test(
                {'base_url': 'https://g.example', 'api_key': 'sk-g'},
                'GET', '/panel/api/accounts/u1/tasks')
        self.assertEqual(out, {'ok': True})
        self.assertEqual(fake.last[2]['Authorization'], 'Bearer sk-g')

    def test_call_without_key_sends_no_authorization(self) -> None:
        fake = _Client(200, {'ok': True})
        with mock.patch.object(config, 'http_client', lambda *a, **k: fake):
            panelapi.call_sync_for_test(
                {'base_url': 'https://g.example', 'api_key': ''}, 'GET', '/panel/api/tasks/queue')
        self.assertNotIn('Authorization', fake.last[2])

    def test_call_maps_upstream_error(self) -> None:
        fake = _Client(422, {'error': 'enterprise session'})
        with mock.patch.object(config, 'http_client', lambda *a, **k: fake):
            with self.assertRaises(panelapi.PanelAPIError) as ctx:
                panelapi.call_sync_for_test(
                    {'base_url': 'https://g.example', 'api_key': ''},
                    'POST', '/panel/api/accounts/u1/tasks/auto', {'task_code': 'x'})
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn('enterprise', ctx.exception.message)

    def test_call_maps_transport_error(self) -> None:
        class Boom:
            async def __aenter__(self):
                raise OSError('connection refused')
            async def __aexit__(self, *exc):
                return False
        with mock.patch.object(config, 'http_client', lambda *a, **k: Boom()):
            with self.assertRaises(panelapi.PanelAPIError) as ctx:
                panelapi.call_sync_for_test(
                    {'base_url': 'https://g.example', 'api_key': ''}, 'GET', '/panel/api/tasks/queue')
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn('connection refused', ctx.exception.message)


if __name__ == '__main__':
    unittest.main()
