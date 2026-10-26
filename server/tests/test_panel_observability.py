"""core 面板只读观测代理（迁移 Phase 2 / Task 8-9）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security  # noqa: E402
from server.routers import panel as panel_router  # noqa: E402
from server.services import panelapi  # noqa: E402


class PanelObservabilityTest(unittest.TestCase):
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
        self.identity = {'username': 'viewer', 'role': 'viewer'}
        self.app = FastAPI()
        self.app.include_router(panel_router.router)
        self.app.dependency_overrides[security.current_user] = lambda: self.identity
        self.c = TestClient(self.app)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        (config.DB_PATH, config.USERS_FILE, config.AUTH_DIR) = self._orig

    def _proxy(self, path: str, want_path: str, body: dict):
        async def fake_call(up, method, call_path, payload=None, timeout=30.0):
            assert method == 'GET'
            assert call_path == want_path, call_path
            return body
        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.get(path)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_packages(self) -> None:
        body = {'accounts': [{'uid': 'u1', 'remain': 10, 'size': 20, 'packages': []}]}
        got = self._proxy('/api/panel/packages', '/panel/api/packages', body)
        self.assertEqual(got['accounts'][0]['uid'], 'u1')

    def test_usage_remaining_clamps_hours(self) -> None:
        body = {'window_hours': 1440, 'budgets': [], 'rows': []}
        got = self._proxy('/api/panel/usage/remaining?hours=99999',
                          '/panel/api/usage/remaining?hours=1440', body)
        self.assertEqual(got['window_hours'], 1440)

    def test_school_vouchers(self) -> None:
        body = {'accounts': [{'uid': 'u1', 'vouchers': []}]}
        got = self._proxy('/api/panel/school/vouchers', '/panel/api/school/vouchers', body)
        self.assertEqual(got['accounts'][0]['uid'], 'u1')

    def test_model_probes(self) -> None:
        body = {'probes': {}, 'exists': False}
        got = self._proxy('/api/panel/model_probes', '/panel/api/model_probes', body)
        self.assertFalse(got['exists'])

    def test_overview_proxies_core_path(self) -> None:
        body = {
            'version': '1.2.3', 'total': 2, 'healthy': 1,
            'model_locks': [{'model': 'm', 'realm': 'cn', 'state': 'locked'}],
            'accounts': [{'uid': 'u1', 'file': 'a.json'}],
        }
        got = self._proxy('/api/panel/overview', '/panel/api/overview', body)
        self.assertEqual(got, body)
        self.assertEqual(got['model_locks'][0]['model'], 'm')
        self.assertEqual(got['accounts'][0]['uid'], 'u1')

    def test_request_metrics_proxies_core_path(self) -> None:
        body = {
            'completed': 3, 'succeeded': 2, 'failed': 1,
            'recent': [{'model': 'm', 'ok': True, 'status': 200}],
            'archive': {'enabled': False, 'files': 0, 'bytes': 0},
        }
        got = self._proxy('/api/panel/request_metrics', '/panel/api/request_metrics', body)
        self.assertEqual(got, body)
        self.assertTrue(got['recent'][0]['ok'])

    def test_overview_unknown_group_404(self) -> None:
        r = self.c.get('/api/panel/overview?upstream_id=99999')
        self.assertEqual(r.status_code, 404, r.text)
        self.assertIn('分组不存在', r.json()['detail'])

    def test_overview_upstream_502_passthrough(self) -> None:
        async def fake_call(*a, **k):
            raise panelapi.PanelAPIError(502, 'upstream down')
        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.get('/api/panel/overview')
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(r.json()['detail'], 'upstream down')

    def test_request_metrics_upstream_502_passthrough(self) -> None:
        async def fake_call(*a, **k):
            raise panelapi.PanelAPIError(502, 'metrics unavailable')
        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.get('/api/panel/request_metrics')
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(r.json()['detail'], 'metrics unavailable')

    def test_overview_and_metrics_do_not_audit(self) -> None:
        async def fake_call(up, method, call_path, payload=None, timeout=30.0):
            return {'ok': True}
        with mock.patch.object(panelapi, 'call', fake_call), \
                mock.patch.object(security, 'audit') as audit:
            for path in ('/api/panel/overview', '/api/panel/request_metrics'):
                r = self.c.get(path)
                self.assertEqual(r.status_code, 200, r.text)
        audit.assert_not_called()

    def test_overview_non_json_upstream_body_maps_to_502(self) -> None:
        class _BadJSON:
            status_code = 200
            text = ''

            def json(self):
                raise ValueError('Expecting value: line 1 column 1 (char 0)')

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, *a, **k):
                return _BadJSON()

        with mock.patch.object(panelapi, 'resolve_upstream',
                               lambda _id: {'base_url': 'https://g.example', 'api_key': ''}), \
                mock.patch.object(config, 'http_client', lambda *a, **k: _Client()):
            r = self.c.get('/api/panel/overview')
        self.assertEqual(r.status_code, 502, r.text)
        self.assertIn('非 JSON', r.json()['detail'])

    def test_unknown_group_404(self) -> None:
        r = self.c.get('/api/panel/packages?upstream_id=99999')
        self.assertEqual(r.status_code, 404, r.text)

    def test_upstream_error_passthrough(self) -> None:
        async def fake_call(*a, **k):
            raise panelapi.PanelAPIError(502, 'upstream down')
        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.get('/api/panel/model_probes')
        self.assertEqual(r.status_code, 502, r.text)
        self.assertIn('upstream down', r.text)


if __name__ == '__main__':
    unittest.main()
