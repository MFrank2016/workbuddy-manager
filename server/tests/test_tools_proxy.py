"""`/v1/search` 与 `/v1/mcp` 的 manager 代理（迁移 Todo 28，契约 §4）。

钉住的性质（做错了都是静默的）：

  1. 开关关闭 → 纯文本 404（带不带 key 都一样，不暴露端点存在性）。
  2. **全局 IP 规则先于密钥解析**：黑名单 IP + 有效 key、黑名单 IP + 无效 key
     都回 403 `ip_blocked`，且不写 request_logs、不打上游。
  3. `keysvc.validate_tools` = 状态（停用/过期/配额/积分额度）+ IP 关，
     **跳过 realm 与模型白名单**（search/MCP 没有 model 维度）。
  4. 专用限流 60/min（与 chat 的 120/min 分开）：第 61 次 429 + Retry-After，
     且**只写入站 IP 日志**，不写 request_logs、不打上游。
  5. 转发按密钥绑定的上游（base_url + 该上游的 api_key），并**由 manager 设置**
     `X-WB2A-Realm`/`X-WB2A-Profile`——客户端伪造的同名头必须被覆盖。
  6. 成功转发写一行 request_logs（0 token、credit NULL），并 `touch` 让 max_ips
     对 tools 流量真正生效。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, security, upstreamsvc  # noqa: E402

IP = '127.0.0.1'


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"object":"search_result","results":[]}',
                 ctype: str = 'application/json') -> None:
        self.status_code = status
        self.content = body
        self.text = body.decode('utf-8', 'replace')
        self.headers = {'content-type': ctype}


class _FakeClient:
    """替身 httpx 客户端：记录每次 post 的 url/content/headers。"""

    def __init__(self, calls: list[dict], resp) -> None:
        self._calls = calls
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        self._calls.append({'url': url, 'content': content, 'headers': dict(headers or {})})
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (config.DB_PATH, config.USERS_FILE, config.AUTH_DIR, config.STATIC_DIR)
        d = Path(self._tmp.name)
        config.DB_PATH = d / 'tools.db'
        config.USERS_FILE = d / 'users.json'
        config.AUTH_DIR = d / 'auths'
        config.STATIC_DIR = d / 'no-static'
        db._conn = None
        db.connect()
        security.save_users({'secret': 'S' * 64, 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('admin-pw')},
        ], 'api_keys': []})
        self.calls: list[dict] = []
        self.resp = _FakeResponse()
        import server.routers.tools as tools_mod
        self._tools = tools_mod
        self._orig_client = tools_mod.config.http_client
        tools_mod.config.http_client = lambda *a, **k: _FakeClient(self.calls, self.resp)
        # 限流桶是模块级状态，而每个用例的临时库都把 key id 从 1 重开；
        # 不清理会让前一个用例的计数漏到下一个用例里。
        with tools_mod._windows_lock:
            tools_mod._windows.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self._tools.config.http_client = self._orig_client
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

    def _key(self, **kw) -> tuple[str, int]:
        created = keysvc.create_key(name=kw.pop('name', 'tools-key'), **kw)
        return created['key'], int(created['id'])

    def _post(self, path: str, token: str | None, body: str = '{"query":"x"}',
              headers: dict | None = None):
        c = self._client()
        h = {'Content-Type': 'application/json'}
        if token:
            h['Authorization'] = f'Bearer {token}'
        h.update(headers or {})
        return c.post(path, content=body, headers=h)

    def _enable(self, *, search: bool = True, mcp: bool = True) -> None:
        db.set_setting('tools_search_enabled', search)
        db.set_setting('tools_mcp_enabled', mcp)

    def _log_count(self) -> int:
        row = db.query_one('SELECT COUNT(*) AS n FROM request_logs')
        return int(row['n']) if row else 0


class FlagTest(_Base):
    def test_disabled_endpoints_are_404(self) -> None:
        token, _ = self._key()
        for path in ('/v1/search', '/v1/mcp'):
            with self.subTest(path=path):
                r = self._post(path, token)
                self.assertEqual(r.status_code, 404, r.text)
                self.assertEqual(self.calls, [])

    def test_flags_are_independent(self) -> None:
        token, _ = self._key()
        self._enable(search=True, mcp=False)
        self.assertEqual(self._post('/v1/search', token).status_code, 200)
        self.assertEqual(self._post('/v1/mcp', token).status_code, 404)


class IpTest(_Base):
    def test_global_blacklist_blocks_even_without_valid_key(self) -> None:
        token, _ = self._key()
        self._enable()
        db.set_setting('security', {'enabled': True, 'mode': 'blacklist'})
        db.execute('INSERT INTO ip_rules(kind, cidr, note, created_at) '
                   "VALUES('deny', ?, '', ?)", ('127.0.0.1/32', int(time.time())))
        for label, tok in (('valid', token), ('invalid', 'nope'), ('missing', None)):
            with self.subTest(key=label):
                r = self._post('/v1/search', tok)
                self.assertEqual(r.status_code, 403, r.text)
                self.assertEqual(r.json()['error']['code'], 'ip_blocked')
        self.assertEqual(self.calls, [], 'blocked IP must never reach upstream')
        self.assertEqual(self._log_count(), 0, 'blocked IP must not write request_logs')

    def test_key_allowlist_violation(self) -> None:
        token, _ = self._key(ip_allowlist=['10.0.0.0/8'])
        self._enable()
        r = self._post('/v1/search', token)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()['error']['code'], 'ip_not_allowed')
        self.assertEqual(self.calls, [])

    def test_max_ips_violation(self) -> None:
        token, key_id = self._key(max_ips=1)
        self._enable()
        db.execute('INSERT OR IGNORE INTO api_key_ips(key_id, ip, first_seen) VALUES(?,?,?)',
                   (key_id, '10.9.9.9', int(time.time())))
        r = self._post('/v1/search', token)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()['error']['code'], 'too_many_ips')

    def test_success_binds_current_ip_for_max_ips(self) -> None:
        token, key_id = self._key(max_ips=1)
        self._enable()
        self.assertEqual(self._post('/v1/search', token).status_code, 200)
        ips = {r['ip'] for r in db.query('SELECT ip FROM api_key_ips WHERE key_id = ?', (key_id,))}
        self.assertIn(IP, ips, 'tools success must touch() so max_ips covers tools traffic')


class KeyStateTest(_Base):
    def test_disabled_key(self) -> None:
        token, key_id = self._key()
        self._enable()
        keysvc.update_key(key_id, {'enabled': False})
        r = self._post('/v1/search', token)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()['error']['code'], 'key_disabled')

    def test_expired_key(self) -> None:
        token, _ = self._key(expires_at=int(time.time()) - 60)
        self._enable()
        r = self._post('/v1/search', token)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()['error']['code'], 'key_expired')

    def test_quota_exhausted(self) -> None:
        token, key_id = self._key(quota=10)
        self._enable()
        db.execute('UPDATE api_keys SET used_tokens = 10 WHERE id = ?', (key_id,))
        r = self._post('/v1/search', token)
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(r.json()['error']['code'], 'quota_exhausted')

    def test_credit_quota_exhausted(self) -> None:
        token, key_id = self._key(quota_credit=1.0)
        self._enable()
        db.execute('UPDATE api_keys SET used_credit = 1.5 WHERE id = ?', (key_id,))
        r = self._post('/v1/search', token)
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(r.json()['error']['code'], 'credit_quota_exhausted')

    def test_realm_bound_and_model_whitelisted_keys_are_allowed(self) -> None:
        token, _ = self._key(realm='cn', models=['glm-5.2'])
        self._enable()
        r = self._post('/v1/search', token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.calls), 1)


class RateLimitTest(_Base):
    def test_61st_request_is_429_without_upstream_or_request_log(self) -> None:
        token, _ = self._key()
        self._enable()
        for i in range(self._tools.TOOLS_PER_MINUTE):
            r = self._post('/v1/search', token)
            self.assertEqual(r.status_code, 200, f'call {i + 1}: {r.text}')
        before_calls = len(self.calls)
        before_logs = self._log_count()
        r = self._post('/v1/search', token)
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(r.headers.get('retry-after'), '60')
        self.assertEqual(len(self.calls), before_calls, '429 must not reach upstream')
        self.assertEqual(self._log_count(), before_logs, '429 must not write request_logs')


class ForwardTest(_Base):
    def test_forwards_to_default_upstream_with_tenant_headers(self) -> None:
        token, _ = self._key(realm='cn', profile='cn-cli')
        self._enable()
        r = self._post('/v1/search', token,
                       headers={'X-WB2A-Realm': 'global', 'X-WB2A-Profile': 'intl-work'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertTrue(call['url'].endswith('/v1/search'), call['url'])
        # manager 设置的头覆盖客户端伪造值（契约 §4：设置而非透传）
        self.assertEqual(call['headers'].get('X-WB2A-Realm'), 'cn')
        self.assertEqual(call['headers'].get('X-WB2A-Profile'), 'cn-cli')
        # 默认上游没配 api_key 时不发 Authorization（与 gateway 同口径）；
        # 配了就必须是上游自己的 key（见 test_non_default_upstream_is_used）。

    def test_non_default_upstream_is_used(self) -> None:
        up = upstreamsvc.create_upstream(name='grp', base_url='https://group.example',
                                        api_key='sk-group')
        token, _ = self._key(upstream_id=int(up['id']))
        self._enable()
        r = self._post('/v1/mcp', token, body='{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0]['url'].startswith('https://group.example/'), self.calls[0]['url'])
        self.assertEqual(self.calls[0]['headers'].get('Authorization'), 'Bearer sk-group')

    def test_request_log_row_is_zero_token_null_credit(self) -> None:
        token, key_id = self._key()
        self._enable()
        self.assertEqual(self._post('/v1/search', token).status_code, 200)
        row = db.query_one('SELECT * FROM request_logs ORDER BY id DESC LIMIT 1')
        self.assertIsNotNone(row)
        self.assertEqual(row['model'], 'web_search')
        self.assertEqual(int(row['prompt_tokens']), 0)
        self.assertEqual(int(row['completion_tokens']), 0)
        self.assertIsNone(row['credit'])
        self.assertEqual(int(row['key_id']), key_id)

    def test_upstream_error_passthrough_and_logged(self) -> None:
        token, _ = self._key()
        self._enable()
        self.resp = _FakeResponse(status=429, body=b'{"error":{"code":"rate_limit_exceeded"}}')
        r = self._post('/v1/search', token)
        self.assertEqual(r.status_code, 429, r.text)
        row = db.query_one('SELECT status, error FROM request_logs ORDER BY id DESC LIMIT 1')
        self.assertEqual(int(row['status']), 429)
        self.assertIn('rate_limit_exceeded', row['error'] or '')

    def test_transport_error_is_502(self) -> None:
        token, _ = self._key()
        self._enable()
        import httpx
        self.resp = httpx.ConnectError('boom')
        r = self._post('/v1/search', token)
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(r.json()['error']['code'], 'upstream_unreachable')


if __name__ == '__main__':
    unittest.main()
