"""并集目录测试的共用夹具（迁移 Todo 18）。

`FakeUpstreams` 按 **base_url** 分流 —— 多分组部署里最常见的问题是「A 组的
钥匙打到了 B 组」，只有按 base 分流才能证明请求真的发给了各自的地址。
`DbCase` / `ApiCase` 各自准备独立临时库：服务层用例一个库一个实例，API 用例
共用一份库（建 app 成本高），所以 `ApiCase.setUp` 显式清空分组表与开关。
"""
from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from server import config, db, keysvc, security, upstreamsvc
from server.services import modelcatalog


def payload(*ids: str) -> dict:
    """一个分组 /v1/models 的响应体（固定时间戳，便于逐字比对）。"""
    return {'object': 'list',
            'data': [{'id': mid, 'object': 'model', 'created': 1753600000,
                      'owned_by': 'workbuddy', 'name': mid.upper()} for mid in ids]}


class FakeUpstreams:
    """按 **base_url** 分流的假上游：每个分组回自己那份清单。

    这比「按路径回同一份」更接近真实故障面：多分组部署里最常见的问题是
    「A 组的钥匙打到了 B 组」，只有按 base 分流才能证明请求真的发给了各自的地址。
    """

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes          # base_url -> payload dict | Exception
        self.calls: list[dict] = []

    def client(self, *args: object, **kwargs: object) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            # `URL.netloc` 在 httpx 0.28 里是 bytes；用 host 拼出可比对的字符串。
            base = f'{request.url.scheme}://{request.url.host}'
            self.calls.append({
                'base': base,
                'path': request.url.path,
                'auth': request.headers.get('authorization', ''),
            })
            spec = self.routes.get(base)
            if isinstance(spec, Exception):
                raise spec
            if spec is None:
                return httpx.Response(404, json={'error': 'no such upstream'})
            return httpx.Response(200, json=spec)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class DbCase:
    """独立临时库（服务层用例）。"""

    def setUp(self) -> None:  # noqa: N802 (unittest 钩子)
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (config.DB_PATH, config.WB2API_BASE, config.WB2API_KEY)
        config.DB_PATH = Path(self._tmp.name) / 'catalog.db'
        config.WB2API_BASE = 'http://default.invalid'
        config.WB2API_KEY = 'default-key'
        db._conn = None
        db.connect()
        modelcatalog.invalidate()

    def tearDown(self) -> None:  # noqa: N802
        modelcatalog.invalidate()
        try:
            if db._conn is not None:
                db._conn.close()
        finally:
            db._conn = None
            config.DB_PATH, config.WB2API_BASE, config.WB2API_KEY = self._orig
            self._tmp.cleanup()

    def group(self, name: str, base: str, key: str = 'k') -> dict:
        return upstreamsvc.create_upstream(name=name, base_url=base, api_key=key)


class ApiCase:
    """真实 FastAPI app + 临时库（与 retention API 测试同一套路）。"""

    @classmethod
    def setUpClass(cls) -> None:  # noqa: N802
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR,
                     config.WB2API_BASE, config.WB2API_KEY)
        config.DB_PATH = d / 'catalog-api.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.WB2API_BASE = 'http://default.invalid'
        config.WB2API_KEY = 'default-key'
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
    def tearDownClass(cls) -> None:  # noqa: N802
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR,
         config.WB2API_BASE, config.WB2API_KEY) = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:  # noqa: N802
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()
        db.set_setting('catalog_group_merge', False)
        # 这个类共用一份临时库（建 app 的成本高），而分组表是全局状态：
        # 不清掉上一个用例建的分组，并集里就会出现它们 —— 断言会莫名多出 id。
        db.execute('DELETE FROM upstreams')
        modelcatalog.invalidate()

    def tearDown(self) -> None:  # noqa: N802
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()
        modelcatalog.invalidate()

    @contextmanager
    def as_user(self, username: str, password: str):
        r = self.c.post('/api/login', json={'username': username, 'password': password})
        assert r.status_code == 200, r.text
        self.c.cookies.update(dict(r.cookies))
        try:
            yield self.c
        finally:
            self.c.cookies.clear()

    def admin_token(self) -> str:
        with self.as_user('admin', 'admin-pw') as c:
            r = c.post('/api/tokens', json={'name': 'ci', 'scope': 'admin'})
        assert r.status_code == 200, r.text
        return r.json()['token']

    def client_key(self) -> str:
        return keysvc.create_key(name='catalog-client')['key']
