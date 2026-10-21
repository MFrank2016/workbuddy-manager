"""可配置的日志保留策略（`log_retention_overrides`，迁移 C2）。

## 为什么要有这个文件

保留期以前是**编译进代码的常量**：请求日志 90 天、侧表 90 天、审计/入站访问
各 2 万行。部署形态差别很大——个人实例希望留久一点回溯，小机器希望留少一点
省盘——但改常量意味着改代码再重新构建。于是把「用户改过的那几项」存进 settings
表，缺的键继续用常量。

三条性质必须钉住，做错了都是**静默**的（不报错，只是行为不对）：

  1. **稀疏存储**：只存用户真正改过的键。把五个默认值全写进库里，会让「以后
     调整常量」对存量部署不再生效——用户从没表达过意见，却被永久钉在旧值上。
  2. **常量仍是兜底**：没有 override 时必须逐字走旧路径（含 10% 余量、每 500
     次检查一次）。存量部署的磁盘增长曲线不该因为这次改动而改变。
  3. **`ip_max_rows` 有 override 时每次写入都精确裁剪**：用户明确说了「只留 N
     行」，就不能再留 10% 余量——那会让界面上的数字与实际行数对不上。

另外两条是权限与可追溯性：读（GET）任何已登录用户都能看，写（POST）**只对
会话管理员**开放（API Token 一律 403）——改保留期能抹掉痕迹，属于高危操作；
每次保存留一条审计（不含敏感数据）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security  # noqa: E402


class _DbCase(unittest.TestCase):
    """独立临时库；每个用例从干净状态开始。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'retention.db'
        db._conn = None
        db.connect()
        db.set_setting('log_retention_overrides', {})
        db._ip_log_writes = 0
        db._request_log_writes = 0

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _request_log(self, ts: int) -> None:
        db.add_request_log(ts=ts, key_id=None, ip='1.1.1.1', model='m',
                           mapped_model='m', status=200, prompt_tokens=1,
                           completion_tokens=1, latency_ms=1, first_token_ms=None,
                           ua='UA', error=None, stream=1, credit=None, realm='cn')

    def _ip_rows(self) -> int:
        row = db.query_one('SELECT COUNT(*) AS n FROM ip_access_logs')
        return int(row['n']) if row else 0

    def _stored(self) -> dict:
        return db.get_setting('log_retention_overrides', {})


class OverrideAffectsPruneTest(_DbCase):
    """`db._prune_*` 必须读 override，而不是只用裸常量。"""

    def test_request_days_override_prunes_cross_day_fixture(self) -> None:
        """设置 request_days=7 后，8 天前的请求日志被清、1 天前的保留。"""
        db.set_setting('log_retention_overrides', {'request_days': 7})
        now = int(time.time())
        self._request_log(now - 8 * 86400)   # 超出 7 天
        self._request_log(now - 1 * 86400)   # 保留期内

        db._prune_request_logs()

        rows = db.query('SELECT ts FROM request_logs')
        self.assertEqual(len(rows), 1, f'override 没生效或删多了：{rows}')
        self.assertGreater(int(rows[0]['ts']), now - 8 * 86400)

    def test_hourly_and_task_days_override(self) -> None:
        db.set_setting('log_retention_overrides', {'hourly_days': 1, 'task_days': 1})
        now = int(time.time())
        old_day = time.strftime('%Y-%m-%d', time.localtime(now - 3 * 86400))
        db.execute('INSERT INTO usage_hourly(day,hour,key_id,model) VALUES(?,?,?,?)',
                   (old_day, 1, 0, 'm'))
        db.execute('INSERT INTO task_logs(ts,uid,kind,dedup_key) VALUES(?,?,?,?)',
                   (now - 3 * 86400, 'u', 'checkin', 'old'))
        db.execute('INSERT INTO task_logs(ts,uid,kind,dedup_key) VALUES(?,?,?,?)',
                   (now, 'u', 'checkin', 'new'))

        db._prune_side_tables()

        self.assertEqual(db.query('SELECT day FROM usage_hourly'), [],
                         'hourly_days override 没生效')
        keys = {r['dedup_key'] for r in db.query('SELECT dedup_key FROM task_logs')}
        self.assertEqual(keys, {'new'}, 'task_days override 没生效')

    def test_audit_max_rows_override_keeps_newest(self) -> None:
        db.set_setting('log_retention_overrides', {'audit_max_rows': 2})
        for i in range(5):
            db.execute('INSERT INTO audit_logs(ts,actor,action) VALUES(?,?,?)',
                       (1700000000 + i, 'admin', f'act{i}'))
        db._prune_side_tables()
        actions = [r['action'] for r in db.query(
            'SELECT action FROM audit_logs ORDER BY id ASC')]
        self.assertEqual(actions, ['act3', 'act4'])


class ConstantsStillFallbackTest(_DbCase):
    """没有 override 时，常量仍然是唯一事实来源（测试仍可 patch 它们）。"""

    def test_audit_rows_fall_back_to_patched_constant(self) -> None:
        for i in range(8):
            db.execute('INSERT INTO audit_logs(ts,actor,action) VALUES(?,?,?)',
                       (1700000000 + i, 'admin', f'act{i}'))
        with mock.patch.object(db, '_AUDIT_LOG_MAX_ROWS', 3):
            db._prune_side_tables()
        actions = [r['action'] for r in db.query(
            'SELECT action FROM audit_logs ORDER BY id ASC')]
        self.assertEqual(actions, ['act5', 'act6', 'act7'])

    def test_request_days_falls_back_to_patched_constant(self) -> None:
        now = int(time.time())
        with mock.patch.object(db, '_REQUEST_LOG_RETAIN_DAYS', 5):
            self._request_log(now - 6 * 86400)
            self._request_log(now - 1 * 86400)
            db._prune_request_logs()
        self.assertEqual(len(db.query('SELECT ts FROM request_logs')), 1,
                         '没有 override 时应继续用（可 patch 的）常量')


def _legacy_ip_count(max_rows: int, every: int, writes: int) -> int:
    """旧实现的算术复刻（无 override 的基线）。

    逐字照抄 `add_ip_access_log` 原来的行为：每 `every` 次写入检查一次，超出时
    删到 `max + max//10` 以下（留 10% 余量）。用它当「基线」的参考实现，避免
    在测试里写死一个从实现倒推出来的数字。
    """
    n = 0
    counter = 0
    for _ in range(writes):
        n += 1
        counter += 1
        if counter >= every:
            counter = 0
            if n > max_rows:
                n -= (n - max_rows + max_rows // 10)
    return n


class IpOverrideExactTest(_DbCase):
    """`ip_max_rows` override：每次写入都精确裁剪；无 override 时逐字走旧路径。"""

    def test_override_keeps_exactly_max_rows(self) -> None:
        """11 次插入、上限 10 → 恰好 10 行（不留 10% 余量）。"""
        db.set_setting('log_retention_overrides', {'ip_max_rows': 10})
        for i in range(11):
            db.add_ip_access_log(f'10.0.0.{i}', '/x', True, f'ua{i}')
        self.assertEqual(self._ip_rows(), 10, 'override 路径没有恰好保留 max 行')
        newest = db.query_one('SELECT ua FROM ip_access_logs ORDER BY id DESC LIMIT 1')
        self.assertEqual(newest['ua'], 'ua10', '精确裁剪丢了最新的记录')

    def test_explicit_default_override_uses_exact_path(self) -> None:
        """显式写 `ip_max_rows=20000`（等于默认值）也必须走精确路径。"""
        db.set_setting('log_retention_overrides', {'ip_max_rows': 20000})
        with mock.patch.object(db, 'prune_ip_access_logs') as prune:
            db.add_ip_access_log('1.2.3.4', '/x', True, 'ua')
        prune.assert_called_once_with(exact=True)

    def test_no_override_keeps_legacy_slack_and_counter(self) -> None:
        """无 override：每 500 次检查一次、留 10% 余量 —— 与基线逐字一致。"""
        max_rows, every, writes = 20, 5, 100
        expected = _legacy_ip_count(max_rows, every, writes)
        with mock.patch.object(db, '_IP_ACCESS_LOG_MAX', max_rows), \
                mock.patch.object(db, '_IP_ACCESS_LOG_CHECK_EVERY', every):
            for i in range(writes):
                db.add_ip_access_log(f'10.0.1.{i}', '/x', True, f'ua{i}')
        self.assertEqual(self._ip_rows(), expected,
                         f'无 override 时行为变了（基线 {expected} 行）')
        self.assertLess(expected, max_rows,
                         '基线本身就会删到上限以下（留 10% 余量），测试前提不成立')

    def test_no_override_does_not_prune_every_write(self) -> None:
        """无 override 时不能退化成「每次写入都查一次」。"""
        with mock.patch.object(db, 'prune_ip_access_logs') as prune:
            for _ in range(10):
                db.add_ip_access_log('1.2.3.4', '/x', True, 'ua')
        prune.assert_not_called()

    def test_no_override_counter_triggers_prune_once(self) -> None:
        with mock.patch.object(db, 'prune_ip_access_logs') as prune:
            db._ip_log_writes = 0
            for _ in range(db._IP_ACCESS_LOG_CHECK_EVERY):
                db.add_ip_access_log('1.2.3.4', '/x', True, 'ua')
        prune.assert_called_once_with(exact=False)
        self.assertEqual(db._ip_log_writes, 0, '计数器没有重置')


class RetentionApiTest(unittest.TestCase):
    """HTTP 面：GET/POST 权限、稀疏存储、校验、审计、保存即裁剪。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'retention-api.db'
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
        db.set_setting('log_retention_overrides', {})
        db._ip_log_writes = 0

    def tearDown(self) -> None:
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()

    @contextmanager
    def _as(self, username: str, password: str):
        r = self.c.post('/api/login', json={'username': username, 'password': password})
        self.assertEqual(r.status_code, 200, r.text)
        self.c.cookies.update(dict(r.cookies))
        try:
            yield self.c
        finally:
            self.c.cookies.clear()

    def _mint_admin_token(self) -> str:
        with self._as('admin', 'admin-pw') as c:
            r = c.post('/api/tokens', json={'name': 'ci', 'scope': 'admin'})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['token']

    def _stored(self) -> dict:
        return db.get_setting('log_retention_overrides', {})

    # ── GET ───────────────────────────────────────────────
    def test_viewer_can_read(self) -> None:
        with self._as('guest', 'guest-pw') as c:
            r = c.get('/api/settings/log-retention')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['overrides'], {}, '默认应是空 override（稀疏）')
        self.assertEqual(body['defaults'], {
            'request_days': 90, 'hourly_days': 90, 'task_days': 90,
            'audit_max_rows': 20000, 'ip_max_rows': 20000,
        })
        self.assertEqual(body['effective'], body['defaults'])

    def test_anonymous_cannot_read(self) -> None:
        self.c.cookies.clear()
        self.assertEqual(self.c.get('/api/settings/log-retention').status_code, 401)

    # ── POST 权限 ─────────────────────────────────────────
    def test_viewer_cannot_write(self) -> None:
        with self._as('guest', 'guest-pw') as c:
            r = c.post('/api/settings/log-retention', json={'request_days': 7})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self._stored(), {}, '被拒的请求不能改库')

    def test_api_token_cannot_write(self) -> None:
        token = self._mint_admin_token()
        r = self.c.post('/api/settings/log-retention',
                        json={'request_days': 7},
                        headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn('API Token', r.json().get('detail', ''))
        self.assertEqual(self._stored(), {}, 'API Token 的请求不能改库')

    # ── 稀疏存储 ──────────────────────────────────────────
    def test_only_changed_keys_are_stored(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            r = c.post('/api/settings/log-retention',
                       json={'request_days': 7, 'hourly_days': 90})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored(), {'request_days': 7},
                         '与默认值相同的键不该落库（稀疏存储）')
        self.assertEqual(r.json()['effective']['request_days'], 7)
        self.assertEqual(r.json()['effective']['hourly_days'], 90)

    def test_resetting_to_default_removes_the_key(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            c.post('/api/settings/log-retention', json={'request_days': 7})
            self.assertEqual(self._stored(), {'request_days': 7})
            c.post('/api/settings/log-retention', json={'request_days': 90})
        self.assertEqual(self._stored(), {},
                         '改回默认值后键应被移除，否则常量再调整就对它无效了')

    def test_explicit_default_value_is_preserved_as_override(self) -> None:
        """显式 `ip_max_rows=20000` 仍要落库 —— 它改变的是**路径**（精确裁剪）。"""
        with self._as('admin', 'admin-pw') as c:
            r = c.post('/api/settings/log-retention', json={'ip_max_rows': 20000})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored(), {'ip_max_rows': 20000},
                         '显式等于默认值的 ip_max_rows 必须保留（它切换精确裁剪路径）')

    # ── 校验 ─────────────────────────────────────────────
    def test_invalid_values_are_rejected_and_store_unchanged(self) -> None:
        db.set_setting('log_retention_overrides', {'request_days': 30})
        bad_payloads = [
            {'request_days': -1},          # 下界
            {'request_days': 0},
            {'request_days': 3651},        # 上界
            {'hourly_days': 1.5},          # 非整数
            {'task_days': '7'},            # 字符串
            {'task_days': True},           # bool 是 int 子类，必须拒
            {'audit_max_rows': 0},
            {'audit_max_rows': 1_000_001},
            {'ip_max_rows': -5},
            {'bogus_key': 7},              # 未知键
        ]
        with self._as('admin', 'admin-pw') as c:
            for payload in bad_payloads:
                with self.subTest(payload=payload):
                    r = c.post('/api/settings/log-retention', json=payload)
                    self.assertEqual(r.status_code, 400, f'{payload} 应被拒：{r.text}')
        self.assertEqual(self._stored(), {'request_days': 30},
                         '非法请求不能改库')

    def test_boundaries_are_accepted(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            r = c.post('/api/settings/log-retention', json={
                'request_days': 1, 'hourly_days': 3650,
                'task_days': 3650, 'audit_max_rows': 1, 'ip_max_rows': 1_000_000,
            })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored(), {
            'request_days': 1, 'hourly_days': 3650, 'task_days': 3650,
            'audit_max_rows': 1, 'ip_max_rows': 1_000_000,
        })

    # ── 保存即裁剪 + 审计 ────────────────────────────────
    def test_save_prunes_ip_logs_exactly_once(self) -> None:
        with mock.patch.object(db, 'prune_ip_access_logs') as prune:
            with self._as('admin', 'admin-pw') as c:
                r = c.post('/api/settings/log-retention', json={'ip_max_rows': 10})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(prune.call_count, 1, '保存时应调用一次精确裁剪')
        self.assertEqual(prune.call_args, mock.call(exact=True))

    def test_save_writes_one_audit_row_without_secrets(self) -> None:
        # 同类用例共享同一个库（TestClient 只能建一次 app），而且**登录本身**也会写
        # 一条 login 审计。所以基线必须在登录之后取，否则会多算一条。
        with self._as('admin', 'admin-pw') as c:
            last_id = int(db.query_one(
                'SELECT COALESCE(MAX(id), 0) AS n FROM audit_logs')['n'])
            before = int(db.query_one('SELECT COUNT(*) AS n FROM audit_logs')['n'])
            c.post('/api/settings/log-retention', json={'request_days': 7})
        rows = db.query('SELECT * FROM audit_logs WHERE id > ? ORDER BY id',
                        (last_id,))
        self.assertEqual(len(rows), 1, '每次保存应写且只写一条审计')
        self.assertEqual(int(db.query_one('SELECT COUNT(*) AS n FROM audit_logs')['n']),
                         before + 1, '不该写多余的行')
        self.assertEqual(rows[0]['action'], 'update_log_retention')
        detail = str(rows[0]['detail'])
        self.assertIn('request_days', detail, '审计要说明改了什么')
        for secret in ('password', 'token', 'secret'):
            self.assertNotIn(secret, detail.lower(), '审计不能带敏感数据')


if __name__ == '__main__':
    unittest.main()
