"""core 面板 Phase 3 代理：补偿状态 / 积分历史 / 暂停选号。"""
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


class PanelPhase3Test(unittest.TestCase):
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
        self.identity = {'username': 'admin', 'role': 'admin'}
        self.app = FastAPI()
        self.app.include_router(panel_router.router)
        self.app.dependency_overrides[security.current_user] = lambda: self.identity
        self.c = TestClient(self.app)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        (config.DB_PATH, config.USERS_FILE, config.AUTH_DIR) = self._orig

    def test_compensation_read_viewer(self) -> None:
        self.identity = {'username': 'v', 'role': 'viewer'}

        async def fake_call(up, method, path, body=None, timeout=30.0):
            assert (method, path) == ('GET', '/panel/api/compensation')
            return {'ok': True, 'accounts': [{'uid': 'u1'}]}

        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.get('/api/panel/compensation')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['accounts'][0]['uid'], 'u1')

    def test_claim_all_admin_and_audit(self) -> None:
        audits: list[tuple] = []

        async def fake_call(up, method, path, body=None, timeout=30.0):
            assert (method, path) == ('POST', '/panel/api/compensation_all')
            return {'ok': True, 'summary': {'claimed': 2, 'credit': 700, 'skipped': 1, 'errored': 0}, 'results': []}

        with mock.patch.object(panelapi, 'call', fake_call), \
             mock.patch.object(security, 'audit', lambda *a, **k: audits.append(a)):
            r = self.c.post('/api/panel/compensation/claim-all')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(audits)
        self.assertIn('claimed=2', audits[0][3])

    def test_claim_single_path_and_audit(self) -> None:
        audits: list[tuple] = []

        async def fake_call(up, method, path, body=None, timeout=30.0):
            assert (method, path) == ('POST', '/panel/api/accounts/u-1/compensation')
            return {'ok': True, 'result': {'claimed': True, 'credit': 500, 'skip': ''}}

        with mock.patch.object(panelapi, 'call', fake_call), \
             mock.patch.object(security, 'audit', lambda *a, **k: audits.append(a)):
            r = self.c.post('/api/panel/compensation/u-1/claim')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(audits)
        self.assertIn('credit=500', audits[0][3])

    def test_viewer_cannot_claim(self) -> None:
        self.identity = {'username': 'v', 'role': 'viewer'}
        r = self.c.post('/api/panel/compensation/u1/claim')
        self.assertEqual(r.status_code, 403, r.text)

    def test_credit_history_clamps_and_filters(self) -> None:
        async def fake_call(up, method, path, body=None, timeout=30.0):
            assert method == 'GET'
            assert path == '/panel/api/credit_history?limit=1000&uid=u1', path
            return {'entries': [], 'limit': 1000}

        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.get('/api/panel/credit_history?uid=u1&limit=99999')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['limit'], 1000)

    def test_pause_and_resume_proxy_and_audit(self) -> None:
        calls: list[tuple] = []
        audits: list[tuple] = []

        async def fake_call(up, method, path, body=None, timeout=30.0):
            calls.append((method, path))
            return {'ok': True}

        with mock.patch.object(panelapi, 'call', fake_call), \
             mock.patch.object(security, 'audit', lambda *a, **k: audits.append(a)):
            self.assertEqual(self.c.post('/api/panel/accounts/u1/pause').status_code, 200)
            self.assertEqual(self.c.post('/api/panel/accounts/u1/resume').status_code, 200)
        self.assertEqual(calls, [('POST', '/panel/api/accounts/u1/pause'),
                                 ('POST', '/panel/api/accounts/u1/resume')])
        self.assertEqual([a[1] for a in audits], ['panel_account_pause', 'panel_account_resume'])

    def test_viewer_cannot_pause(self) -> None:
        self.identity = {'username': 'v', 'role': 'viewer'}
        r = self.c.post('/api/panel/accounts/u1/pause')
        self.assertEqual(r.status_code, 403, r.text)

    def test_upstream_error_passthrough(self) -> None:
        async def fake_call(*a, **k):
            raise panelapi.PanelAPIError(502, 'upstream down')
        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.get('/api/panel/credit_history')
        self.assertEqual(r.status_code, 502, r.text)
        self.assertIn('upstream down', r.text)

    def test_unknown_group_404(self) -> None:
        r = self.c.get('/api/panel/compensation?upstream_id=99999')
        self.assertEqual(r.status_code, 404, r.text)


if __name__ == '__main__':
    unittest.main()
