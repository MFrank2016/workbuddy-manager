"""`request_logs.tool_repair` 的写入 / 迁移 / 读取（契约 §5 / Todo 15）。

枚举只有四个值：`none|fixed|failed|recalled`。这里钉住三件事：

  · 旧库升级后必须补出这一列，且**历史记录**读作 `none`（不能是 NULL——
    列声明是 `NOT NULL DEFAULT 'none'`，界面不该为旧数据特判）；
  · `_record` 不传 `tool_repair` 时落 `none`（其余协议的调用点不必逐个改）；
  · `/api/logs` 的返回 dict 带这个字段（Todo 8 的日志抽屉依赖它）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security  # noqa: E402
from server.routers import gateway as G  # noqa: E402

ENUM = {'none', 'fixed', 'failed', 'recalled'}


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / 'old.db'
        self._orig = config.DB_PATH
        self._make_old_db()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _make_old_db(self) -> None:
        conn = sqlite3.connect(self._path)
        conn.execute(
            'CREATE TABLE request_logs ('
            '  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, key_id INTEGER,'
            '  ip TEXT, model TEXT, mapped_model TEXT, status INTEGER DEFAULT 0,'
            '  prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,'
            '  latency_ms INTEGER DEFAULT 0, ua TEXT, error TEXT, stream INTEGER DEFAULT 0,'
            '  first_token_ms INTEGER, credit REAL, realm TEXT);'
        )
        conn.execute('INSERT INTO request_logs(ts, latency_ms, prompt_tokens) VALUES(1, 5, 7)')
        conn.commit()
        conn.close()

    def test_old_db_gains_column_and_history_reads_none(self) -> None:
        config.DB_PATH = self._path
        db._conn = None
        db.connect()
        cols = {r[1] for r in db._conn.execute('PRAGMA table_info(request_logs)')}
        self.assertIn('tool_repair', cols, '升级后缺列，写入会整条失败')
        row = db.query_one('SELECT prompt_tokens, tool_repair FROM request_logs WHERE id = 1')
        self.assertEqual(row['prompt_tokens'], 7, '历史记录不能丢')
        self.assertEqual(row['tool_repair'], 'none', '历史记录应读作 none')

    def test_migration_is_idempotent(self) -> None:
        config.DB_PATH = self._path
        for _ in range(2):
            db._conn = None
            db.connect()
        cols = [r[1] for r in db._conn.execute('PRAGMA table_info(request_logs)')]
        self.assertEqual(cols.count('tool_repair'), 1)


class RecordEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'enum.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _last(self) -> dict:
        return db.query_one('SELECT * FROM request_logs ORDER BY id DESC LIMIT 1')

    def test_default_is_none(self) -> None:
        G._record(None, '1.1.1.1', 'm', '', 200, 1, 1, 5, 'UA', None, False)
        self.assertEqual(self._last()['tool_repair'], 'none')

    def test_each_enum_value_is_written(self) -> None:
        for value in sorted(ENUM):
            with self.subTest(value=value):
                G._record(None, '1.1.1.1', 'm', '', 200, 1, 1, 5, 'UA', None, False,
                          tool_repair=value)
                self.assertEqual(self._last()['tool_repair'], value)

    def test_empty_value_falls_back_to_none(self) -> None:
        """调用方误传空串时不能写空——列默认值是 none，空串会让界面无从判断。"""
        G._record(None, '1.1.1.1', 'm', '', 200, 1, 1, 5, 'UA', None, False,
                  tool_repair='')
        self.assertEqual(self._last()['tool_repair'], 'none')


class LogsApiExposesFieldTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)
        self._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = self._dir / 'logs.db'
        config.USERS_FILE = self._dir / 'users.json'
        config.STATIC_DIR = self._dir / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        self.client = TestClient(app)
        self.token = security.issue_token('admin', 'admin')
        self.client.cookies.set(config.COOKIE_NAME, self.token)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_fixed_is_visible_in_items(self) -> None:
        db.add_request_log(ts=int(time.time()), key_id=None, ip='1.1.1.1', model='m',
                           mapped_model='', status=200, prompt_tokens=1,
                           completion_tokens=1, latency_ms=5, first_token_ms=None,
                           ua='UA', error=None, stream=0, credit=None, realm='cn',
                           tool_repair='fixed')
        r = self.client.get('/api/logs')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['items'][0]['tool_repair'], 'fixed')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
