"""HTTP-level contract tests for the watchdog router.

The service tests in `test_watchdog.py` call functions directly. These tests go
through FastAPI so the dependency wiring (auth), status-code mapping, and audit
side effects are covered too — a broken `Depends(...)` would otherwise pass every
unit test while the endpoint is unusable.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import security  # noqa: E402
from server.routers import watchdog as router  # noqa: E402

ADMIN = {'username': 'admin', 'role': 'admin'}


class WatchdogRouterHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(router.router)
        app.dependency_overrides[security.require_admin] = lambda: ADMIN
        self.app = app
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def _require_unauthenticated(self) -> None:
        self.app.dependency_overrides.pop(security.require_admin, None)

        def unauth() -> dict:
            raise HTTPException(status_code=401, detail='未登录')

        self.app.dependency_overrides[security.current_user] = unauth

    def test_config_and_run_require_authentication(self) -> None:
        self._require_unauthenticated()
        self.assertEqual(self.client.get('/api/watchdog/config').status_code, 401)
        self.assertEqual(self.client.put('/api/watchdog/config', json={}).status_code, 401)
        self.assertEqual(self.client.post('/api/watchdog/run').status_code, 401)

    def test_config_and_run_reject_non_admin(self) -> None:
        def forbidden() -> dict:
            raise HTTPException(status_code=403, detail='需要管理员权限')

        self.app.dependency_overrides[security.require_admin] = forbidden
        self.assertEqual(self.client.get('/api/watchdog/config').status_code, 403)
        self.assertEqual(self.client.put('/api/watchdog/config', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/watchdog/run').status_code, 403)

    def test_get_config_returns_masked_payload(self) -> None:
        cfg = {'mail': {'password': '********'}, 'llm': {'api_key': '********'}}
        with mock.patch.object(router.watchdog, 'public_config', return_value=cfg):
            resp = self.client.get('/api/watchdog/config')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'config': cfg})
        self.assertNotIn('secret', resp.text)

    def test_put_config_saves_and_audits(self) -> None:
        result = {
            'config': {'enabled': True, 'interval_minutes': 60, 'targets': [],
                       'llm': {'enabled': False}, 'mail': {'enabled': False}},
            'warning': '',
        }
        with mock.patch.object(router.watchdog, 'save_config',
                               new=mock.AsyncMock(return_value=result)) as save, \
             mock.patch.object(security, 'audit') as audit, \
             mock.patch.object(router, 'client_ip', return_value='1.2.3.4'):
            resp = self.client.put('/api/watchdog/config', json={'enabled': True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), result)
        save.assert_awaited_once_with({'enabled': True})
        audit.assert_called_once()
        args, kwargs = audit.call_args
        self.assertEqual(args[0], ADMIN)
        self.assertEqual(args[1], 'update_watchdog')
        self.assertIn('enabled=True', args[3])
        self.assertEqual(kwargs['ip'], '1.2.3.4')

    def test_put_config_maps_validation_error_to_400(self) -> None:
        with mock.patch.object(router.watchdog, 'save_config',
                               new=mock.AsyncMock(side_effect=ValueError('interval too small'))):
            resp = self.client.put('/api/watchdog/config', json={'interval_minutes': 1})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('interval too small', resp.json()['detail'])

    def test_run_returns_result_and_audits(self) -> None:
        result = {'ok': True, 'changed': ['plans-usage'], 'targets': []}
        with mock.patch.object(router.watchdog, 'run_once',
                               new=mock.AsyncMock(return_value=result)) as run, \
             mock.patch.object(security, 'audit') as audit, \
             mock.patch.object(router, 'client_ip', return_value='1.2.3.4'):
            resp = self.client.post('/api/watchdog/run')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), result)
        run.assert_awaited_once_with(trigger='manual')
        audit.assert_called_once()
        args, kwargs = audit.call_args
        self.assertEqual(args[1], 'run_watchdog')
        self.assertIn('changed=plans-usage', args[3])
        self.assertEqual(kwargs['ip'], '1.2.3.4')

    def test_run_reports_empty_change_set(self) -> None:
        result = {'ok': True, 'changed': [], 'targets': []}
        with mock.patch.object(router.watchdog, 'run_once',
                               new=mock.AsyncMock(return_value=result)), \
             mock.patch.object(security, 'audit') as audit:
            resp = self.client.post('/api/watchdog/run')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('changed=无', audit.call_args.args[3])


if __name__ == '__main__':
    unittest.main()
