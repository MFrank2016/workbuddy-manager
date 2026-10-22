"""网关接入模型路由表：**先鉴权后映射** + 租户隔离（迁移契约 §2）。

## 为什么顺序必须换

旧实现是「先 `_map_model`，再 `_authorize`」。路由表按 `(public_id, realm,
key_group)` 匹配，而 `key_group` 只有**解析出密钥之后**才知道 —— 先映射就只能按
NULL 分组查，绑定分组的密钥会看到本不属于它的路由（租户隔离直接失效）。

所以本文件钉住的核心不变量：**映射发生在密钥解析之后**，`_authorize` 返回
`(key, ip, mapped, err)`；调用点不再自己预先映射。

## 本文件覆盖

  · flag off → 与旧 `model_map` 逐字一致（golden 的模型名不变）；
  · flag on → 按路由改写；realm 用**模型名**判定（`global:` 前缀 → global）；
  · 租户隔离：绑定组 1 的密钥看不到组 2 的行；未绑定密钥只吃 NULL 组行；
  · `/v1/models`：`keep_original=1` 保留上游原名 + 别名，`0` 只留别名；
    白名单写别名时列表与调用一致；
  · `_upstream_headers(upstream, key)`：空值不写头，`key=None` 两个头都不写；
  · 真实链路（TestClient）：flag off 上游收到原名、flag on 收到路由目标、
    组隔离生效、`/v1/messages` flag-off 请求体与 golden 逐字一致。
"""
from __future__ import annotations

import asyncio
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
from server.routers import gateway as G  # noqa: E402
from server.routers import playground as PG  # noqa: E402
from server.tests import golden_capture  # noqa: E402

ROUTE_INSERT = (
    'INSERT INTO model_routes(public_id, target_model, realm, upstream_group_id, '
    'credits_override, keep_original, enabled, created_at, updated_at) '
    'VALUES(?, ?, ?, ?, NULL, ?, 1, 1, 1)'
)

MODELS = {'object': 'list', 'data': [
    {'id': 'glm-5.2'},
    {'id': 'deepseek-v4.1-flash'},
    {'id': 'global:gpt-5.6-sol'},
]}

CHAT = {'id': 'chatcmpl-test', 'object': 'chat.completion', 'model': 'upstream',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                     'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}

GOLDEN = Path(__file__).resolve().parent / 'golden'


def _route(public_id: str, target: str, realm: str = '', group: int | None = None,
           keep_original: int = 1) -> None:
    db.execute(ROUTE_INSERT, (public_id, target, realm, group, keep_original))


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'routes.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        finally:
            db._conn = None
            config.DB_PATH = self._orig
            self._tmp.cleanup()


class FlagOffParityTest(_DbCase):
    """`model_routes_enabled=false` → 完全走旧 `model_map`（逐字一致）。"""

    def test_route_table_is_ignored_when_flag_is_off(self) -> None:
        db.set_setting('model_map', {'alias': 'legacy-target'})
        _route('alias', 'route-target')
        self.assertEqual(G._map_model('alias', {'upstream_id': 1}), 'legacy-target')

    def test_golden_model_name_is_unchanged_when_flag_is_off(self) -> None:
        """golden 采集时的模型名（无映射）必须原样返回。"""
        body = json.loads((GOLDEN / 'chat_request_body.json').read_text(encoding='utf-8'))
        self.assertEqual(G._map_model(body['model'], {'upstream_id': None}), body['model'])

    def test_default_flag_is_off(self) -> None:
        self.assertIs(db.get_setting('model_routes_enabled', False), False)

    def test_legacy_fallback_still_applies(self) -> None:
        db.set_setting('model_map', {'*': 'fallback-target'})
        self.assertEqual(G._map_model('unknown-name', None), 'fallback-target')


class FlagOnResolutionTest(_DbCase):
    def setUp(self) -> None:
        super().setUp()
        db.set_setting('model_routes_enabled', True)

    def test_route_rewrites_public_id_to_target(self) -> None:
        _route('alias', 'target-model')
        self.assertEqual(G._map_model('alias', None), 'target-model')

    def test_unmapped_name_passes_through_when_table_not_empty(self) -> None:
        _route('alias', 'target-model')
        self.assertEqual(G._map_model('other', None), 'other')

    def test_global_prefixed_public_id_matches_global_realm_row(self) -> None:
        """realm 由**模型名**判定：`global:` 前缀的 public_id 命中 global 行。"""
        _route('global:alias', 'global:gpt-5.6-sol', 'global')
        self.assertEqual(G._map_model('global:alias', None), 'global:gpt-5.6-sol')

    def test_empty_table_falls_back_to_legacy_model_map(self) -> None:
        """契约 §2 第 6 级：表为空（升级窗口）仍沿用旧映射。"""
        db.set_setting('model_map', {'alias': 'legacy-target'})
        self.assertEqual(G._map_model('alias', None), 'legacy-target')

    def test_none_model_is_passed_through(self) -> None:
        _route('alias', 'target-model')
        self.assertIsNone(G._map_model(None, None))


class TenantIsolationTest(_DbCase):
    """绑定分组的密钥只能看到本组行与 NULL 行 —— 路由不得突破密钥绑定分组。"""

    def setUp(self) -> None:
        super().setUp()
        db.set_setting('model_routes_enabled', True)

    def test_unbound_key_only_sees_null_group_rows(self) -> None:
        _route('alias', 'group-1-target', group=1)
        self.assertEqual(G._map_model('alias', {'upstream_id': None}), 'alias')

    def test_key_sees_own_group_row(self) -> None:
        _route('alias', 'group-1-target', group=1)
        self.assertEqual(G._map_model('alias', {'upstream_id': 1}), 'group-1-target')

    def test_key_never_sees_another_groups_row(self) -> None:
        _route('alias', 'group-2-target', group=2)
        self.assertEqual(G._map_model('alias', {'upstream_id': 1}), 'alias')

    def test_null_group_row_applies_to_every_key(self) -> None:
        _route('alias', 'any-group-target')
        self.assertEqual(G._map_model('alias', {'upstream_id': 1}), 'any-group-target')
        self.assertEqual(G._map_model('alias', {'upstream_id': 2}), 'any-group-target')

    def test_own_group_row_wins_over_null_row(self) -> None:
        _route('alias', 'any-group-target')
        _route('alias', 'group-1-target', group=1)
        self.assertEqual(G._map_model('alias', {'upstream_id': 1}), 'group-1-target')


class ModelsListingTest(_DbCase):
    """`/v1/models` 必须体现路由别名与 `keep_original`。"""

    def setUp(self) -> None:
        super().setUp()
        db.set_setting('model_routes_enabled', True)

    def _ids(self, key: dict | None) -> list[str]:
        out = G._scope_models(MODELS, key)
        return [m['id'] for m in out['data']]

    def test_keep_original_keeps_upstream_id_and_alias(self) -> None:
        _route('my-alias', 'glm-5.2')
        ids = self._ids({'realm': '', 'models': []})
        self.assertIn('glm-5.2', ids, 'keep_original=1 时上游原名必须保留')
        self.assertIn('my-alias', ids, '别名必须出现在清单里')

    def test_keep_original_zero_keeps_only_public_id(self) -> None:
        _route('my-alias', 'glm-5.2', keep_original=0)
        ids = self._ids({'realm': '', 'models': []})
        self.assertNotIn('glm-5.2', ids, 'keep_original=0 时上游原名必须撤下')
        self.assertEqual(ids.count('my-alias'), 1)

    def test_alias_entry_carries_target_metadata(self) -> None:
        payload = {'object': 'list', 'data': [{'id': 'glm-5.2', 'name': 'GLM', 'created': 1}]}
        _route('my-alias', 'glm-5.2')
        out = G._scope_models(payload, {'realm': '', 'models': []})
        entry = [m for m in out['data'] if m['id'] == 'my-alias'][0]
        self.assertEqual(entry['name'], 'GLM')
        self.assertEqual(entry['created'], 1)

    def test_whitelist_accepts_the_alias(self) -> None:
        """白名单写别名时：列表给出别名，调用放行（issue #46 的不变量）。"""
        _route('my-alias', 'glm-5.2')
        key = {'realm': '', 'models': ['my-alias'], 'enabled': True, 'expires_at': 0,
               'quota': 0, 'quota_credit': 0, 'used_tokens': 0, 'used_credit': 0,
               'ip_allowlist': [], 'max_ips': 0, 'id': 1}
        self.assertEqual(self._ids(key), ['my-alias'])
        self.assertIsNone(keysvc.validate(key, '1.2.3.4', 'my-alias',
                                          mapped_model='glm-5.2'))

    def test_alias_outside_whitelist_is_not_listed(self) -> None:
        """白名单里没写别名 → 不列出（列了也调不通）。"""
        _route('my-alias', 'glm-5.2')
        key = {'realm': '', 'models': ['deepseek-v4.1-flash']}
        self.assertEqual(self._ids(key), ['deepseek-v4.1-flash'])

    def test_flag_off_listing_unchanged(self) -> None:
        db.set_setting('model_routes_enabled', False)
        _route('my-alias', 'glm-5.2')
        ids = self._ids({'realm': '', 'models': []})
        self.assertEqual(ids, ['glm-5.2', 'deepseek-v4.1-flash', 'global:gpt-5.6-sol'])

    def test_foreign_group_route_is_not_listed(self) -> None:
        _route('group2-alias', 'glm-5.2', group=2)
        self.assertEqual(self._ids({'realm': '', 'models': [], 'upstream_id': 1}),
                         ['glm-5.2', 'deepseek-v4.1-flash', 'global:gpt-5.6-sol'])

    def test_realm_scoped_route_not_listed_for_other_realm(self) -> None:
        _route('cn-only', 'glm-5.2', 'cn')
        _route('global-only', 'global:gpt-5.6-sol', 'global')
        self.assertIn('cn-only', self._ids({'realm': 'cn', 'models': []}))
        self.assertNotIn('global-only', self._ids({'realm': 'cn', 'models': []}))


class UpstreamHeadersTest(unittest.TestCase):
    """`_upstream_headers` 是 `X-WB2A-Profile` / `X-WB2A-Realm` 的唯一写入点。"""

    UP = {'base_url': 'http://upstream.invalid', 'api_key': 'k'}

    def test_profile_and_derived_realm_are_written(self) -> None:
        h = G._upstream_headers(self.UP, {'profile': 'intl-cli'})
        self.assertEqual(h['X-WB2A-Profile'], 'intl-cli')
        self.assertEqual(h['X-WB2A-Realm'], 'global')

    def test_explicit_key_realm_wins_over_profile(self) -> None:
        h = G._upstream_headers(self.UP, {'profile': 'intl-cli', 'realm': 'cn'})
        self.assertEqual(h['X-WB2A-Realm'], 'cn')

    def test_empty_profile_and_realm_write_no_headers(self) -> None:
        h = G._upstream_headers(self.UP, {'profile': '', 'realm': ''})
        self.assertNotIn('X-WB2A-Profile', h)
        self.assertNotIn('X-WB2A-Realm', h)

    def test_missing_key_writes_no_headers(self) -> None:
        h = G._upstream_headers(self.UP)
        self.assertNotIn('X-WB2A-Profile', h)
        self.assertNotIn('X-WB2A-Realm', h)

    def test_unknown_profile_does_not_invent_a_realm(self) -> None:
        h = G._upstream_headers(self.UP, {'profile': 'weird-profile'})
        self.assertEqual(h['X-WB2A-Profile'], 'weird-profile')
        self.assertNotIn('X-WB2A-Realm', h)


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
        self.calls.append({'path': request.url.path, 'body': body,
                           'headers': dict(request.headers)})
        if request.method == 'GET' and request.url.path.endswith('/v1/models'):
            return httpx.Response(200, json=MODELS)
        return httpx.Response(200, json=CHAT)

    def client(self, *args, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def models(self) -> list[str]:
        posts = [c for c in self.calls if c['path'].endswith('/v1/chat/completions')]
        return [c['body']['model'] for c in posts]


class GatewayRequestPathTest(unittest.TestCase):
    """真实链路：TestClient → 路由 → 鉴权 → 映射 → 上游。"""

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
        from server.main import app
        self.client = TestClient(app)
        self.rec = _Recorder()

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

    def _post(self, token: str, model: str, path: str = '/v1/chat/completions',
              body: dict | None = None) -> httpx.Response:
        payload = body or {'model': model,
                           'messages': [{'role': 'user', 'content': 'hi'}]}
        payload['model'] = model
        with mock.patch.object(config, 'http_client', side_effect=self.rec.client):
            return self.client.post(path, headers={'Authorization': f'Bearer {token}'},
                                    json=payload)

    def test_flag_off_sends_the_requested_model(self) -> None:
        db.set_setting('model_map', {'alias': 'legacy-target'})
        r = self._post(self._token(), 'alias')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.rec.models(), ['legacy-target'])

    def test_flag_off_golden_body_is_byte_identical(self) -> None:
        """flag off 时上游收到的 body 与 golden 逐字一致。"""
        golden = json.loads((GOLDEN / 'chat_request_body.json').read_text(encoding='utf-8'))
        r = self._post(self._token(), golden['model'], body=dict(golden))
        self.assertEqual(r.status_code, 200, r.text)
        sent = [c for c in self.rec.calls if c['path'].endswith('/v1/chat/completions')][0]
        self.assertEqual(sent['body'], golden)

    def test_flag_on_sends_the_routed_target(self) -> None:
        db.set_setting('model_routes_enabled', True)
        _route('alias', 'routed-target')
        r = self._post(self._token(), 'alias')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.rec.models(), ['routed-target'])

    def test_group_isolation_on_the_real_path(self) -> None:
        """密钥绑定组 1：组 2 的路由不生效；组 1 的路由生效。"""
        db.set_setting('model_routes_enabled', True)
        g1 = upstreamsvc.create_upstream('g1', 'http://upstream.invalid', api_key='k1')
        g2 = upstreamsvc.create_upstream('g2', 'http://upstream.invalid', api_key='k2')
        _route('alias', 'group-2-target', group=g2['id'])
        token = self._token(upstream_id=g1['id'])

        r = self._post(token, 'alias')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.rec.models(), ['alias'], '组 2 的路由对组 1 密钥可见了')

        _route('alias', 'group-1-target', group=g1['id'])
        self.rec.calls.clear()
        r = self._post(token, 'alias')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.rec.models(), ['group-1-target'])

    def test_unbound_key_ignores_group_rows(self) -> None:
        db.set_setting('model_routes_enabled', True)
        g1 = upstreamsvc.create_upstream('g1', 'http://upstream.invalid', api_key='k1')
        _route('alias', 'group-1-target', group=g1['id'])
        r = self._post(self._token(), 'alias')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.rec.models(), ['alias'])

    def test_models_listing_keeps_original_with_keep_original(self) -> None:
        db.set_setting('model_routes_enabled', True)
        _route('my-alias', 'glm-5.2')
        with mock.patch.object(config, 'http_client', side_effect=self.rec.client):
            r = self.client.get('/v1/models',
                                headers={'Authorization': f'Bearer {self._token()}'})
        self.assertEqual(r.status_code, 200, r.text)
        ids = [m['id'] for m in r.json()['data']]
        self.assertIn('glm-5.2', ids)
        self.assertIn('my-alias', ids)

    def test_repro_every_listed_alias_is_callable_over_http(self) -> None:
        """缺陷 1 的原始 HTTP 复现：清单里的每个 id 都必须能 POST 成功。

        历史脏行 `public_id='alias' + realm='global'`（国际版密钥）：修复前清单
        会列出 `alias`，而 POST 必然 400 `realm_mismatch`、零次出站。现在它
        不再被列出，且清单里剩下的每一个 id 都能真的调通。
        """
        db.set_setting('model_routes_enabled', True)
        _route('alias', 'global:gpt-5.6-sol', 'global')
        token = self._token(realm='global')

        with mock.patch.object(config, 'http_client', side_effect=self.rec.client):
            listing = self.client.get(
                '/v1/models', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(listing.status_code, 200, listing.text)
        ids = [m['id'] for m in listing.json()['data']]
        self.assertNotIn('alias', ids, '列出了一个调用必然 400 的别名（issue #46）')

        for mid in ids:
            with self.subTest(model=mid):
                r = self._post(token, mid)
                self.assertEqual(r.status_code, 200,
                                 f'清单给出的 {mid!r} 调用失败：{r.text}')
        # 出站必须是用真正发往上游的名字（清单里那两条原样透传）
        self.assertEqual(self.rec.models(), ids)

    def test_anthropic_flag_off_body_matches_golden(self) -> None:
        """`/v1/messages` flag-off 的上游请求体与 golden 逐字一致。"""
        golden = json.loads((GOLDEN / 'anthropic_request_body.json').read_text(encoding='utf-8'))
        r = self._post(self._token(), golden['model'], path='/v1/messages', body=dict(golden))
        self.assertEqual(r.status_code, 200, r.text)
        sent = [c for c in self.rec.calls if c['path'].endswith('/v1/chat/completions')][0]
        self.assertEqual(sent['body'], golden)


class PlaygroundUnaffectedTest(unittest.TestCase):
    """测试台没有下游密钥（`key=None`）：不写租户头，也不能因签名变化 500。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'pg.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def test_headers_have_no_tenant_scope(self) -> None:
        h = G._upstream_headers(upstreamsvc.default_upstream())
        self.assertNotIn('X-WB2A-Profile', h)
        self.assertNotIn('X-WB2A-Realm', h)

    def test_chat_still_works(self) -> None:
        class FakeResp:
            status_code = 200
            headers = {'content-type': 'application/json'}

            async def aread(self):
                return json.dumps(CHAT).encode()

            async def aclose(self):
                return None

            async def aiter_bytes(self):
                yield json.dumps(CHAT).encode()

        class FakeClient:
            def build_request(self, *a, **k):
                return object()

            async def send(self, *a, **k):
                return FakeResp()

            async def aclose(self):
                return None

        body = PG.ChatIn(model='glm-5.2', messages=[{'role': 'user', 'content': 'hi'}],
                         stream=False)

        class FakeReq:
            headers = {'user-agent': 'test'}

        async def run():
            with mock.patch.object(PG.config, 'http_client', return_value=FakeClient()), \
                    mock.patch.object(PG, 'client_ip', return_value='10.0.0.1'):
                return await PG.chat(body, request=FakeReq(), user={'username': 'admin'})

        resp = asyncio.run(run())
        self.assertEqual(resp.status_code, 200)


class AnthropicGoldenReproductionTest(unittest.TestCase):
    """重新采集一次：`/v1/messages` 的 flag-off 请求体必须与提交的 golden 一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.captured = golden_capture.build_payloads()

    def test_anthropic_body_matches_committed_golden(self) -> None:
        committed = (GOLDEN / 'anthropic_request_body.json').read_text(encoding='utf-8')
        self.assertEqual(self.captured['anthropic_request_body.json'], committed)


if __name__ == '__main__':
    unittest.main()
