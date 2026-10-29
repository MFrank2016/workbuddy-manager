"""`request_logs.effort` 的写入 / 迁移 / 读取（issue #267）。

思考强度（reasoning_effort）只做**旁路展示**：

  · 旧库升级后必须补出 `effort` 列，且历史记录读作 NULL（界面显示「—」，
    不按模型默认档推断——否则会把「客户端没传」误读成「要了某个档位」）；
  · `_record` 不传 effort 时落 NULL，传了则清洗 + 截断后落库；
  · `/api/logs` 的返回 dict 带这个字段（日志页表格 / 卡片依赖它）。
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

    def test_old_db_gains_column_and_history_reads_null(self) -> None:
        config.DB_PATH = self._path
        db._conn = None
        db.connect()
        cols = {r[1] for r in db._conn.execute('PRAGMA table_info(request_logs)')}
        self.assertIn('effort', cols, '升级后缺列，写入会整条失败')
        row = db.query_one('SELECT prompt_tokens, effort FROM request_logs WHERE id = 1')
        self.assertEqual(row['prompt_tokens'], 7, '历史记录不能丢')
        self.assertIsNone(row['effort'], '历史记录没有该字段，必须读作 NULL')

    def test_migration_is_idempotent(self) -> None:
        config.DB_PATH = self._path
        for _ in range(2):
            db._conn = None
            db.connect()
        cols = [r[1] for r in db._conn.execute('PRAGMA table_info(request_logs)')]
        self.assertEqual(cols.count('effort'), 1)


class RecordEffortTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'effort.db'
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

    def test_absent_effort_is_null(self) -> None:
        G._record(None, '1.1.1.1', 'm', '', 200, 1, 1, 5, 'UA', None, False)
        self.assertIsNone(self._last()['effort'], '没传就是 NULL，不能猜默认档')

    def test_explicit_effort_is_written(self) -> None:
        for value in ('low', 'medium', 'high', 'max'):
            with self.subTest(value=value):
                G._record(None, '1.1.1.1', 'm', '', 200, 1, 1, 5, 'UA', None, False,
                          effort=value)
                self.assertEqual(self._last()['effort'], value)

    def test_effort_is_cleaned_and_truncated(self) -> None:
        G._record(None, '1.1.1.1', 'm', '', 200, 1, 1, 5, 'UA', None, False,
                  effort='high\ninjected-line')
        got = self._last()['effort']
        self.assertNotIn('\n', got)
        self.assertLessEqual(len(got), 16)


class LogsApiExposesEffortTest(unittest.TestCase):
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

    def _add(self, effort: str | None) -> None:
        db.add_request_log(ts=int(time.time()), key_id=None, ip='1.1.1.1', model='m',
                           mapped_model='', status=200, prompt_tokens=1,
                           completion_tokens=1, latency_ms=5, first_token_ms=None,
                           ua='UA', error=None, stream=0, credit=None, realm='cn',
                           effort=effort)

    def test_effort_is_visible_in_items(self) -> None:
        self._add('high')
        r = self.client.get('/api/logs')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['items'][0]['effort'], 'high')

    def test_old_rows_expose_null(self) -> None:
        self._add(None)
        r = self.client.get('/api/logs')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()['items'][0]['effort'])
