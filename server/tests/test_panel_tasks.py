"""core 面板任务代理（迁移 Phase 1 / Task 2-4）。"""
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


class PanelTasksTest(unittest.TestCase):
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

    def test_get_tasks_proxies(self) -> None:
        async def fake_call(up, method, path, body=None, timeout=30.0):
            assert method == 'GET'
            assert path == '/panel/api/accounts/u1/tasks'
            return {'ok': True, 'tasks': [{'task_code': 't1', 'current': 1, 'target': 2}]}
        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.get('/api/panel/accounts/u1/tasks')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['tasks'][0]['task_code'], 't1')

    def test_unknown_group_404(self) -> None:
        r = self.c.get('/api/panel/accounts/u1/tasks?upstream_id=99999')
        self.assertEqual(r.status_code, 404, r.text)

    def test_viewer_cannot_accept(self) -> None:
        self.identity = {'username': 'v', 'role': 'viewer'}
        r = self.c.post('/api/panel/accounts/u1/tasks/accept', json={'task_codes': ['t1']})
        self.assertEqual(r.status_code, 403, r.text)

    def test_accept_requires_codes(self) -> None:
        r = self.c.post('/api/panel/accounts/u1/tasks/accept', json={})
        self.assertEqual(r.status_code, 400, r.text)

    def test_accept_proxies_and_audits(self) -> None:
        async def fake_call(up, method, path, body=None, timeout=30.0):
            assert method == 'POST'
            assert path == '/panel/api/accounts/u1/tasks/accept'
            assert body == {'task_codes': ['t1']}
            return {'ok': True}
        audits = []
        with mock.patch.object(panelapi, 'call', fake_call), \
             mock.patch.object(security, 'audit', lambda *a, **k: audits.append(a)):
            r = self.c.post('/api/panel/accounts/u1/tasks/accept', json={'task_codes': ['t1']})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(audits)

    def test_claim_passes_upstream_422(self) -> None:
        async def fake_call(*a, **k):
            raise panelapi.PanelAPIError(422, 'enterprise session')
        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.post('/api/panel/accounts/u1/tasks/claim', json={'task_code': 't1'})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn('enterprise', r.text)

    def test_scan_all_proxies(self) -> None:
        async def fake_call(up, method, path, body=None, timeout=30.0):
            assert path == '/panel/api/tasks/scan_all'
            return {'ok': True, 'pending_count': 3, 'accounts': []}
        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.post('/api/panel/tasks/scan_all')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['pending_count'], 3)

    def test_run_queue_clamps_concurrency(self) -> None:
        seen = {}
        async def fake_call(up, method, path, body=None, timeout=30.0):
            seen.update(body or {})
            return {'ok': True, 'started': True}
        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.post('/api/panel/tasks/run_queue', json={'concurrency': 99, 'growth': True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen['concurrency'], 4)

    def test_queue_viewer_can_read(self) -> None:
        self.identity = {'username': 'v', 'role': 'viewer'}
        async def fake_call(*a, **k):
            return {'running': False, 'items': []}
        with mock.patch.object(panelapi, 'call', fake_call):
            r = self.c.get('/api/panel/tasks/queue')
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == '__main__':
    unittest.main()
