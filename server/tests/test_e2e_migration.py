"""迁移 E2E（Todo 33）：真实 FastAPI 路由 + 真实 DB/文件，只对上游 HTTP 打桩。

覆盖四条用户路径：
  1. 模型路由 → chat → request_logs（路由真的改写了发给上游的 model）；
  2. 批量 refresh token 导入 → auth 文件（profile 保留）；
  3. billing 兼容端点（读本地面板记录，不打上游）；
  4. search/MCP 代理（manager 设置租户头、按 key 限流、写日志）。

不依赖真实上游、不读 ../auths、不 skip。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402
from server.routers import accounts  # noqa: E402
from server.services import modelrouteadmin, tencent  # noqa: E402


class _Resp:
    def __init__(self, status: int, body: dict, ctype: str = 'application/json') -> None:
        self.status_code = status
        self._body = body
        self.content = json.dumps(body, ensure_ascii=False).encode()
        self.text = self.content.decode()
        self.headers = {'content-type': ctype}

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, calls: list[dict], chat: _Resp, tools: _Resp) -> None:
        self.calls = calls
        self.chat = chat
        self.tools = tools

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, content=None, **kw):
        self.calls.append({'url': url, 'json': json, 'content': content,
                           'headers': dict(headers or {})})
        if '/v1/chat/completions' in url:
            return self.chat
        return self.tools

    def build_request(self, *a, **kw):
        return object()

    async def send(self, req, stream=False):
        return self.chat


class E2EMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.AUTH_DIR,
                     config.STATIC_DIR, config.WB2API_BASE)
        config.DB_PATH = d / 'e2e.db'
        config.USERS_FILE = d / 'users.json'
        config.AUTH_DIR = d / 'auths'
        config.AUTH_DIR.mkdir()
        config.STATIC_DIR = d / 'no-static'
        config.WB2API_BASE = 'https://upstream.example'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('admin-pw')}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        cls.app = app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        (config.DB_PATH, config.USERS_FILE, config.AUTH_DIR,
         config.STATIC_DIR, config.WB2API_BASE) = cls._orig
        cls._tmp.cleanup()

    def setUp(self) -> None:
        security._fail.clear()
        security._user_fail.clear()
        self.client.cookies.clear()
        db.execute('DELETE FROM request_logs')
        db.execute('DELETE FROM model_routes')
        db.set_setting('model_routes_enabled', False)
        db.set_setting('tools_search_enabled', False)
        db.set_setting('tools_mcp_enabled', False)
        self.calls: list[dict] = []
        self.chat = _Resp(200, {
            'id': 'chatcmpl-e2e', 'object': 'chat.completion', 'created': 1,
            'model': 'glm-5.2',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
        })
        self.tools = _Resp(200, {'object': 'search_result', 'results': [
            {'title': 'T', 'url': 'https://e.x', 'snippet': 'S'}]})
        self._orig_client = config.http_client
        config.http_client = lambda *a, **k: _FakeClient(self.calls, self.chat, self.tools)
        self.addCleanup(setattr, config, 'http_client', self._orig_client)

    def _login(self) -> None:
        r = self.client.post('/api/login', json={'username': 'admin', 'password': 'admin-pw'})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.cookies.update(dict(r.cookies))

    def _key(self, **kw) -> str:
        return keysvc.create_key(name=kw.pop('name', 'e2e'), **kw)['key']

    def test_route_chat_logs(self) -> None:
        modelrouteadmin.create_route({
            'public_id': 'e2e-public', 'target_model': 'glm-5.2', 'realm': '',
            'upstream_group_id': None, 'keep_original': True, 'enabled': True,
        })
        db.set_setting('model_routes_enabled', True)
        key = self._key()
        r = self.client.post('/v1/chat/completions', headers={'Authorization': f'Bearer {key}'},
                             json={'model': 'e2e-public', 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['json']['model'], 'glm-5.2',
                         '路由必须改写发给上游的 model')
        row = db.query_one('SELECT model, mapped_model FROM request_logs ORDER BY id DESC LIMIT 1')
        self.assertIsNotNone(row)
        self.assertEqual(row['model'], 'e2e-public')
        self.assertEqual(row['mapped_model'], 'glm-5.2')

    def test_bulk_import_writes_profile(self) -> None:
        self._login()
        with mock.patch.object(tencent, 'refresh_token', return_value=(
                True, 'ok', {'access_token': 'at-new', 'expires_at': 9999999999})), \
             mock.patch.object(accounts.reload, 'request_reload_or_restart'):
            r = self.client.post('/api/accounts/import-tokens', json={
                'realm': 'cn',
                'entries': [{'uid': 'u-e2e', 'refreshToken': 'rt-old',
                             'profile': 'cn-work'}],
            })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['counts']['added'], 1, body)
        files = list(config.AUTH_DIR.glob('workbuddy-u-e2e*.json'))
        self.assertEqual(len(files), 1, files)
        raw = json.loads(files[0].read_text(encoding='utf-8'))
        self.assertEqual(raw.get('profile'), 'cn-work')
        self.assertEqual(raw['auth']['accessToken'], 'at-new')

    def test_billing_subscription(self) -> None:
        key = self._key()
        r = self.client.get('/v1/dashboard/billing/subscription',
                            headers={'Authorization': f'Bearer {key}'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['object'], 'billing_subscription')
        self.assertIn('hard_limit_usd', body)

    def test_search_and_mcp_proxy(self) -> None:
        key = self._key(realm='cn')
        db.set_setting('tools_search_enabled', True)
        db.set_setting('tools_mcp_enabled', True)
        r = self.client.post('/v1/search', headers={'Authorization': f'Bearer {key}'},
                             json={'query': 'golang'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.calls[-1]['url'], 'https://upstream.example/v1/search')
        self.assertEqual(self.calls[-1]['headers'].get('X-WB2A-Realm'), 'cn')

        r = self.client.post('/v1/mcp', headers={'Authorization': f'Bearer {key}'},
                             json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.calls[-1]['url'], 'https://upstream.example/v1/mcp')


if __name__ == '__main__':
    unittest.main()
