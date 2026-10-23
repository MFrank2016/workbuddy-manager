"""Responses 投影 / 工具修复设置（迁移 Todo 16）的 HTTP 面。

钉住四件事：
  1. viewer 能读、不能写；匿名 401；API Token 写 403（与其它设置端点同口径）。
  2. 部分更新：只写提交了的键；非法值 400 且不改库；未知键 400（拼错不能静默成功）。
  3. 校验边界：mode 只认 passthrough|balanced；max_item_bytes 只认 0 或 >=256；
     两个修复开关只认真布尔。
  4. 写成功会留一条审计，且审计文本不含请求体原文。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security  # noqa: E402


class ResponsesSettingsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'responses-settings.db'
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
        for key in ('responses_projection_mode', 'responses_projection_max_item_bytes',
                    'tool_repair', 'tool_repair_recall'):
            db.set_setting(key, None)
        db.clear_audit_logs()

    def tearDown(self) -> None:
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()

    def _as(self, username: str, password: str):
        r = self.c.post('/api/login', json={'username': username, 'password': password})
        self.assertEqual(r.status_code, 200, r.text)
        self.c.cookies.update(dict(r.cookies))
        return self.c

    def _stored(self) -> dict:
        return {
            'mode': db.get_setting('responses_projection_mode', None),
            'max': db.get_setting('responses_projection_max_item_bytes', None),
            'repair': db.get_setting('tool_repair', None),
            'recall': db.get_setting('tool_repair_recall', None),
        }

    def test_defaults_are_passthrough_and_off(self) -> None:
        c = self._as('guest', 'guest-pw')
        r = c.get('/api/settings/responses')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {
            'projection_mode': 'passthrough',
            'max_item_bytes': 40000,
            'tool_repair': False,
            'tool_repair_recall': False,
        })

    def test_anonymous_cannot_read(self) -> None:
        self.c.cookies.clear()
        self.assertEqual(self.c.get('/api/settings/responses').status_code, 401)

    def test_viewer_cannot_write(self) -> None:
        c = self._as('guest', 'guest-pw')
        r = c.post('/api/settings/responses', json={'tool_repair': True})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIsNone(self._stored()['repair'], '被拒的请求不能改库')

    def test_admin_partial_update(self) -> None:
        c = self._as('admin', 'admin-pw')
        r = c.post('/api/settings/responses', json={
            'projection_mode': 'balanced',
            'max_item_bytes': 512,
            'tool_repair': True,
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['projection_mode'], 'balanced')
        self.assertEqual(r.json()['max_item_bytes'], 512)
        self.assertTrue(r.json()['tool_repair'])
        self.assertFalse(r.json()['tool_repair_recall'], '未提交的键保持默认')
        # 再改一个键，不能把其它键冲掉
        r = c.post('/api/settings/responses', json={'tool_repair_recall': True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['projection_mode'], 'balanced')
        self.assertEqual(r.json()['max_item_bytes'], 512)
        self.assertTrue(r.json()['tool_repair_recall'])

    def test_invalid_values_are_rejected_and_store_unchanged(self) -> None:
        c = self._as('admin', 'admin-pw')
        cases = [
            {'projection_mode': 'custom'},
            {'max_item_bytes': 100},
            {'max_item_bytes': -1},
            {'max_item_bytes': '512'},
            {'max_item_bytes': True},
            {'tool_repair': 'true'},
            {'tool_repair': 1},
            {'unknown_key': 1},
        ]
        for body in cases:
            with self.subTest(body=body):
                r = c.post('/api/settings/responses', json=body)
                self.assertEqual(r.status_code, 400, f'{body} -> {r.status_code} {r.text}')
        self.assertEqual(self._stored(), {'mode': None, 'max': None,
                                          'repair': None, 'recall': None})

    def test_zero_max_bytes_is_valid(self) -> None:
        c = self._as('admin', 'admin-pw')
        r = c.post('/api/settings/responses', json={'max_item_bytes': 0})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['max_item_bytes'], 0)

    def test_audit_written_without_body(self) -> None:
        c = self._as('admin', 'admin-pw')
        c.post('/api/settings/responses', json={'projection_mode': 'balanced',
                                                'max_item_bytes': 512})
        rows = db.list_audit_logs(limit=10)
        hit = [r for r in rows if r['action'] == 'update_responses_settings']
        self.assertTrue(hit, f'缺少审计记录: {rows}')
        detail = hit[0]['detail']
        self.assertIn('projection_mode=balanced', detail)
        self.assertNotIn('{"', detail, '审计不该记录请求体原文')


if __name__ == '__main__':
    unittest.main()
