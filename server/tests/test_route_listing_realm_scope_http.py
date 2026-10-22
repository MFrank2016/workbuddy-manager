"""真实链路（TestClient）：`/v1/models` 的每个 id 都必须 POST 成功。

矩阵覆盖 shadow 行与跨版本 `'*'` 兜底两类缺陷（说明见
`test_route_listing_realm_scope.py`），维度是 版本 × 绑定分组 × `keep_original`。
判据走真实 HTTP：清单 → 逐条 POST，断言 200 且错误码不是 `realm_mismatch`。

为什么单独一个模块：纯函数矩阵（清单与 `keysvc.validate` 同口径）与真实链路
（鉴权 → 映射 → 上游）失败模式不同，前者定位到判据、后者定位到接线；混在一起
会让模块越过单文件可维护的边界。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security, upstreamsvc  # noqa: E402
from server.tests.test_route_listing_realm_scope import (  # noqa: E402
    PAYLOAD, ROUTE_INSERT, SHADOW, STAR)

CHAT_URL = '/v1/chat/completions'
CHAT = {'id': 'chatcmpl-test', 'object': 'chat.completion', 'model': 'upstream',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                     'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
ADMIN_ROUTES = '/api/settings/model-routes'


class _Recorder:
    """记录发往上游的请求，按 fixture 回响应（不碰网络）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content.decode('utf-8'))
            except ValueError:
                body = None
        self.calls.append({'path': request.url.path, 'body': body})
        if request.method == 'GET' and request.url.path.endswith('/v1/models'):
            return httpx.Response(200, json=PAYLOAD)
        return httpx.Response(200, json=CHAT)

    def client(self, *args, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class HttpListingInvariantTest(unittest.TestCase):
    """真实链路：`GET /v1/models` 的每个 id 都必须 POST 成功。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig = {
            'DB_PATH': config.DB_PATH, 'USERS_FILE': config.USERS_FILE,
            'STATIC_DIR': config.STATIC_DIR, 'WB2API_BASE': config.WB2API_BASE,
        }
        config.DB_PATH = root / 'e2e.db'
        config.USERS_FILE = root / 'users.json'
        config.STATIC_DIR = root / 'no-static'
        config.USERS_FILE.write_text(
            json.dumps({'secret': 'S' * 64, 'users': [], 'api_keys': []}),
            encoding='utf-8')
        db._conn = None
        db.connect()
        db.set_setting('model_routes_enabled', True)
        from server.main import app
        self.client = TestClient(app)
        self.rec = _Recorder()
        self.group = upstreamsvc.create_upstream(
            'g', 'http://upstream.invalid', api_key='k')['id']

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        for name, value in self._orig.items():
            setattr(config, name, value)
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _token(self, **kw) -> str:
        return keysvc.create_key('t', **kw)['key']

    def _listing(self, token: str) -> list[str]:
        with mock.patch.object(config, 'http_client', side_effect=self.rec.client):
            r = self.client.get('/v1/models',
                                headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 200, r.text)
        return [m['id'] for m in r.json()['data']]

    def _post(self, token: str, model: str) -> httpx.Response:
        with mock.patch.object(config, 'http_client', side_effect=self.rec.client):
            return self.client.post(
                CHAT_URL, headers={'Authorization': f'Bearer {token}'},
                json={'model': model,
                      'messages': [{'role': 'user', 'content': 'hi'}]})

    def _seed(self, shadow: bool, star: bool, keep_original: int,
              group: int | None) -> None:
        db.execute('DELETE FROM model_routes')
        rows = []
        if shadow:
            rows.append((SHADOW[0], SHADOW[1], '', group, keep_original))
        if star:
            rows.append((STAR[0], STAR[1], '', group, keep_original))
        for row in rows:
            db.execute(ROUTE_INSERT, row)

    def test_every_advertised_id_is_callable_over_http(self) -> None:
        """两类缺陷行 × 版本 × 绑定分组 × keep_original：列出即可调。"""
        for shadow, star in ((True, False), (False, True), (True, True)):
            for keep_original in (1, 0):
                for realm in ('cn', 'global'):
                    for group in (None, self.group):
                        with self.subTest(shadow=shadow, star=star,
                                          keep_original=keep_original,
                                          realm=realm, group=group):
                            self._seed(shadow, star, keep_original, group)
                            token = self._token(realm=realm, upstream_id=group)
                            ids = self._listing(token)
                            self.assertTrue(ids)
                            for mid in ids:
                                r = self._post(token, mid)
                                self.assertEqual(
                                    r.status_code, 200,
                                    f'清单给出的 {mid!r} 调用失败：{r.text}')
                                code = r.json().get('error', {}).get('code')
                                self.assertNotEqual(
                                    code, 'realm_mismatch',
                                    f'清单给出的 {mid!r} 被版本检查打回：{r.text}')

    def test_shadow_row_is_refused_by_the_admin_write_path(self) -> None:
        """写入口径：同一行经管理面 POST 现在应 400，而不是静默存进去。"""
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('pw')}],
            'api_keys': [],
        }), encoding='utf-8')
        security._fail.clear()
        security._user_fail.clear()
        login = self.client.post('/api/login',
                                 json={'username': 'admin', 'password': 'pw'})
        self.assertEqual(login.status_code, 200, login.text)
        self.client.cookies.update(dict(login.cookies))
        try:
            r = self.client.post(ADMIN_ROUTES, json={
                'public_id': SHADOW[0], 'target_model': SHADOW[1], 'realm': ''})
        finally:
            self.client.cookies.clear()
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(db.query('SELECT id FROM model_routes'), [])


if __name__ == '__main__':
    unittest.main()
