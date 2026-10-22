"""OpenAI 兼容 billing 端点 + billing settings（迁移 Todo 17，契约 §3）。

## 这个端点解决什么

OpenAI 生态的客户端（各类用量面板 / 计费插件）会去读
`/v1/dashboard/billing/subscription` 与 `/v1/dashboard/billing/usage` 来展示
「总额度、剩余、每天花了多少」。本面板的额度单位是**积分（credit）**，不是
美元，所以这两个端点只做一件事：**用 `billing_credit_price_usd` 把积分换算成
美元 / 美分**，不改变任何计费口径。

## 必须钉住的性质（做错了都是静默的）

  1. `quota_credit=0`（不限）时 limits / balance 输出 **null**，客户端应把 null
     当「不限」——糊成 0 会让用户以为额度用完了。
  2. `billing_usd_rate` **只**参与 `billing_currency=cny` 的
     `codebuddy_balance_cny`，绝不参与任何 USD 字段（否则汇率一改，美元账单
     跟着变，对不上）。
  3. usage 的 `total_usage` 单位是**美分**，且等于**逐项** `round_half_up`
     之后的和：3 条 credit=1.5、price=0.03 → 每条 4.5 美分进 5，总计 15。
     先全局求和再舍入会得到 14（13.5 进 14），分项与总计对不上。
  4. 配额耗尽 / 已过期的密钥**仍要能读账单**（这正是用户最需要看的时候）。
  5. IP 管控照常生效：全局黑名单 403 `ip_blocked`，密钥白名单 400
     `ip_not_allowed`，`max_ips` 400 `too_many_ips`（保持既有语义）。
  6. 每个 billing 请求写**一行** request_logs（0 token、credit NULL），与
     `/v1/usage` 同口径——否则监控轮询在运维眼里是隐形的。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, security  # noqa: E402
from server.services import billing  # noqa: E402
from server.tests import settings_defaults  # noqa: E402

IP = '127.0.0.1'


class _Base(unittest.TestCase):
    """每个用例一份临时库（与 test_key_usage 同一套路）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (config.DB_PATH, config.USERS_FILE, config.AUTH_DIR,
                      config.STATIC_DIR)
        d = Path(self._tmp.name)
        config.DB_PATH = d / 'billing.db'
        config.USERS_FILE = d / 'users.json'
        config.AUTH_DIR = d / 'auths'
        config.STATIC_DIR = d / 'no-static'
        db._conn = None
        db.connect()
        security.save_users({'secret': 'S' * 64, 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('admin-pw')},
            {'username': 'guest', 'role': 'viewer',
             'pwd_hash': security.make_hash('guest-pw')},
        ], 'api_keys': []})
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        (config.DB_PATH, config.USERS_FILE, config.AUTH_DIR,
         config.STATIC_DIR) = self._orig
        self._tmp.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        from server.main import app
        # 真实来源 IP：TestClient 默认对端是 'testclient'，IP 规则无从判定。
        return TestClient(app, client=(IP, 50000))

    def _login(self, username: str, password: str):
        c = self._client()
        r = c.post('/api/login', json={'username': username, 'password': password})
        assert r.status_code == 200, r.text
        c.cookies.update(dict(r.cookies))
        return c

    def _key(self, **kw) -> tuple[str, int]:
        created = keysvc.create_key(name=kw.pop('name', 'billing-key'), **kw)
        return created['key'], int(created['id'])

    def _get(self, path: str, token: str, **kw):
        return self._client().get(path, headers={'Authorization': f'Bearer {token}'}, **kw)


class SubscriptionTest(_Base):
    def test_fields_and_credit_to_usd_conversion(self) -> None:
        token, key_id = self._key(quota_credit=30)
        db.execute('UPDATE api_keys SET used_credit = 5 WHERE id = ?', (key_id,))
        r = self._get('/v1/dashboard/billing/subscription', token)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['object'], 'billing_subscription')
        self.assertIs(body['has_payment_method'], False)
        self.assertIs(body['canceled'], False)
        self.assertIsNone(body['canceled_at'])
        self.assertIsNone(body['access_until'])
        self.assertEqual(body['soft_limit_usd'], 0)
        # 30 * 0.03 = 0.9；剩余 (30-5) * 0.03 = 0.75
        self.assertEqual(body['hard_limit_usd'], 0.9)
        self.assertEqual(body['system_hard_limit_usd'], 0.9)
        self.assertEqual(body['codebuddy_balance_usd'], 0.75)
        self.assertNotIn('codebuddy_balance_cny', body,
                         'usd 币种不该输出 cny 字段')

    def test_unlimited_quota_reports_null_not_zero(self) -> None:
        token, _ = self._key(quota_credit=0)
        r = self._get('/v1/dashboard/billing/subscription', token)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsNone(body['hard_limit_usd'])
        self.assertIsNone(body['system_hard_limit_usd'])
        self.assertIsNone(body['codebuddy_balance_usd'])

    def test_cny_adds_converted_balance_and_never_touches_usd(self) -> None:
        token, key_id = self._key(quota_credit=30)
        db.execute('UPDATE api_keys SET used_credit = 5 WHERE id = ?', (key_id,))
        db.set_setting('billing_currency', 'cny')
        db.set_setting('billing_usd_rate', 7.15)
        body = self._get('/v1/dashboard/billing/subscription', token).json()
        self.assertEqual(body['hard_limit_usd'], 0.9)
        self.assertEqual(body['codebuddy_balance_usd'], 0.75)
        self.assertEqual(body['codebuddy_balance_cny'], 5.36,
                         '0.75 * 7.15 = 5.3625 → round half up 到 5.36')

    def test_cny_field_present_even_when_unlimited(self) -> None:
        """币种是 cny 时字段**总是**在；不限额度时值为 null（形状不随配额变）。"""
        token, _ = self._key(quota_credit=0)
        db.set_setting('billing_currency', 'cny')
        body = self._get('/v1/dashboard/billing/subscription', token).json()
        self.assertIn('codebuddy_balance_cny', body)
        self.assertIsNone(body['codebuddy_balance_cny'])
        self.assertIsNone(body['codebuddy_balance_usd'])

    def test_usd_rate_does_not_affect_usd_fields(self) -> None:
        token, _ = self._key(quota_credit=30)
        db.set_setting('billing_usd_rate', 100)
        body = self._get('/v1/dashboard/billing/subscription', token).json()
        self.assertEqual(body['hard_limit_usd'], 0.9)
        self.assertEqual(body['codebuddy_balance_usd'], 0.9)

    def test_access_until_is_key_expiry(self) -> None:
        token, _ = self._key(quota_credit=1, expires_at=1790000000)
        body = self._get('/v1/dashboard/billing/subscription', token).json()
        self.assertEqual(body['access_until'], 1790000000)

    def test_balance_never_negative(self) -> None:
        token, key_id = self._key(quota_credit=10)
        db.execute('UPDATE api_keys SET used_credit = 99 WHERE id = ?', (key_id,))
        body = self._get('/v1/dashboard/billing/subscription', token).json()
        self.assertEqual(body['codebuddy_balance_usd'], 0.0)

    def test_exhausted_and_expired_keys_can_still_read_billing(self) -> None:
        token, key_id = self._key(quota_credit=10, quota=100,
                                  expires_at=int(time.time()) - 60)
        db.execute('UPDATE api_keys SET used_credit = 10, used_tokens = 100 '
                   'WHERE id = ?', (key_id,))
        r = self._get('/v1/dashboard/billing/subscription', token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['hard_limit_usd'], 0.3)

    def test_missing_and_invalid_key_are_rejected(self) -> None:
        c = self._client()
        self.assertEqual(c.get('/v1/dashboard/billing/subscription').status_code, 401)
        r = c.get('/v1/dashboard/billing/subscription',
                  headers={'Authorization': 'Bearer wbk_not-a-real-key'})
        self.assertEqual(r.status_code, 401)
        self.assertIn('invalid_api_key', r.text)


class IpGuardTest(_Base):
    def test_global_blacklist_is_403_ip_blocked(self) -> None:
        token, _ = self._key(quota_credit=1)
        db.set_setting('security', {'enabled': True, 'mode': 'blacklist'})
        db.execute('INSERT INTO ip_rules(kind, cidr, note, created_at) '
                   "VALUES('deny', ?, '', ?)", ('127.0.0.1/32', int(time.time())))
        r = self._get('/v1/dashboard/billing/subscription', token)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()['error']['code'], 'ip_blocked')

    def test_key_allowlist_violation_is_400_ip_not_allowed(self) -> None:
        token, _ = self._key(quota_credit=1, ip_allowlist=['10.0.0.0/8'])
        r = self._get('/v1/dashboard/billing/subscription', token)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()['error']['code'], 'ip_not_allowed')

    def test_max_ips_violation_is_400_too_many_ips(self) -> None:
        token, key_id = self._key(quota_credit=1, max_ips=1)
        db.execute('INSERT OR IGNORE INTO api_key_ips(key_id, ip, first_seen) '
                   'VALUES(?, ?, ?)', (key_id, '10.9.9.9', int(time.time())))
        r = self._get('/v1/dashboard/billing/subscription', token)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()['error']['code'], 'too_many_ips')

    def test_validate_ip_only_ignores_quota_and_expiry(self) -> None:
        """Todo 28 的 `validate_tools` 要在它之上补 enabled/expiry/quota。"""
        _, key_id = self._key(quota_credit=1, quota=1)
        key = keysvc.get_key(key_id)
        self.assertIsNone(keysvc.validate_ip_only(key, IP))

        db.execute('UPDATE api_keys SET used_credit = 99, used_tokens = 99, '
                   'enabled = 0 WHERE id = ?', (key_id,))
        disabled = keysvc.get_key(key_id)
        self.assertIsNone(keysvc.validate_ip_only(disabled, IP),
                          'validate_ip_only 只做 IP 层，不能替调用方做额度判定')

    def test_validate_ip_only_returns_key_layer_rejections(self) -> None:
        _, key_id = self._key(quota_credit=1, ip_allowlist=['10.0.0.0/8'])
        reason = keysvc.validate_ip_only(keysvc.get_key(key_id), IP)
        self.assertEqual(reason.code, 'ip_not_allowed')
        self.assertEqual(reason.status, 400)

    def test_validate_still_rejects_disabled_keys(self) -> None:
        """抽出 IP 层不能改变 `validate` 的既有语义。"""
        _, key_id = self._key(quota_credit=1)
        db.execute('UPDATE api_keys SET enabled = 0 WHERE id = ?', (key_id,))
        reason = keysvc.validate(keysvc.get_key(key_id), IP, 'glm-5.2')
        self.assertEqual(reason.code, 'key_disabled')


class UsageTest(_Base):
    def _seed(self, day: str, model: str, credit: float, key_id: int) -> None:
        db.execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, '
            'completion_tokens, credit, realm) VALUES(?,?,?,?,?,?,?,?)',
            (day, key_id, model, 1, 10, 5, credit, db.realm_of_model(model)),
        )

    def test_total_usage_sums_per_item_rounding(self) -> None:
        """3 条 credit=1.5、price=0.03 → 每条 4.5 美分进 5，总计 15。"""
        token, key_id = self._key(quota_credit=100)
        day = db.day_of()
        for model in ('model-a', 'model-b', 'model-c'):
            self._seed(day, model, 1.5, key_id)
        r = self._get('/v1/dashboard/billing/usage', token)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['object'], 'list')
        self.assertEqual(len(body['daily_costs']), 1)
        entry = body['daily_costs'][0]
        self.assertEqual(entry['timestamp'], db.day_start_ts(day))
        items = entry['line_items']
        self.assertEqual([i['name'] for i in items], ['model-a', 'model-b', 'model-c'])
        self.assertEqual([i['cost'] for i in items], [5, 5, 5])
        self.assertEqual(body['total_usage'], 15)
        self.assertEqual(body['total_usage'], sum(i['cost'] for i in items))

    def test_rounding_is_half_up(self) -> None:
        token, key_id = self._key(quota_credit=100)
        self._seed(db.day_of(), 'm', 0.5, key_id)   # 0.5*0.03*100 = 1.5 → 2
        body = self._get('/v1/dashboard/billing/usage', token).json()
        self.assertEqual(body['total_usage'], 2)

    def test_default_range_is_last_30_days(self) -> None:
        token, key_id = self._key(quota_credit=100)
        now = int(time.time())
        self._seed(db.day_of(now), 'recent', 1.0, key_id)
        self._seed(db.day_of(now - 40 * 86400), 'old', 1.0, key_id)
        body = self._get('/v1/dashboard/billing/usage', token).json()
        names = [i['name'] for d in body['daily_costs'] for i in d['line_items']]
        self.assertIn('recent', names)
        self.assertNotIn('old', names, '默认窗口是最近 30 天')

    def test_explicit_range_filters_rows(self) -> None:
        token, key_id = self._key(quota_credit=100)
        self._seed('2026-01-10', 'in-range', 1.0, key_id)
        self._seed('2026-01-01', 'too-old', 1.0, key_id)
        body = self._get(
            '/v1/dashboard/billing/usage?start_date=2026-01-05&end_date=2026-01-15',
            token).json()
        names = [i['name'] for d in body['daily_costs'] for i in d['line_items']]
        self.assertEqual(names, ['in-range'])

    def test_invalid_dates_are_400(self) -> None:
        token, _ = self._key(quota_credit=1)
        for qs in ('start_date=2026/01/01', 'end_date=2026-13-01',
                   'start_date=2026-01-10&end_date=2026-01-01',
                   'start_date=2025-01-01&end_date=2026-03-01'):
            with self.subTest(qs=qs):
                r = self._get(f'/v1/dashboard/billing/usage?{qs}', token)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()['error']['code'], 'invalid_date_range')

    def test_quota_exhausted_key_still_gets_usage(self) -> None:
        token, key_id = self._key(quota_credit=1)
        db.execute('UPDATE api_keys SET used_credit = 5 WHERE id = ?', (key_id,))
        self._seed(db.day_of(), 'm', 1.0, key_id)
        r = self._get('/v1/dashboard/billing/usage', token)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['total_usage'], 3)   # 1*0.03*100 = 3

    def test_zero_credit_rows_produce_no_line_items(self) -> None:
        token, key_id = self._key(quota_credit=100)
        self._seed(db.day_of(), 'free', 0.0, key_id)
        body = self._get('/v1/dashboard/billing/usage', token).json()
        self.assertEqual(body['daily_costs'], [])
        self.assertEqual(body['total_usage'], 0)

    def test_other_keys_usage_is_invisible(self) -> None:
        mine, _ = self._key(quota_credit=100, name='mine')
        _, other_id = self._key(quota_credit=100, name='other')
        self._seed(db.day_of(), 'their-model', 9.0, other_id)
        body = self._get('/v1/dashboard/billing/usage', mine).json()
        self.assertEqual(body['total_usage'], 0)


class RequestLogTest(_Base):
    def test_each_billing_request_writes_one_zero_token_row(self) -> None:
        token, key_id = self._key(quota_credit=10)
        self._get('/v1/dashboard/billing/subscription', token)
        self._get('/v1/dashboard/billing/usage', token)
        rows = db.query('SELECT key_id, status, prompt_tokens, completion_tokens, '
                        'credit FROM request_logs ORDER BY id')
        self.assertEqual(len(rows), 2, '每个 billing 请求一行 request_logs')
        for row in rows:
            self.assertEqual(row['key_id'], key_id)
            self.assertEqual(row['status'], 200)
            self.assertEqual((row['prompt_tokens'], row['completion_tokens']), (0, 0))
            self.assertIsNone(row['credit'], 'credit 必须 NULL（不是 0）')

        used = db.query_one('SELECT used_tokens, used_credit FROM api_keys WHERE id = ?',
                            (key_id,))
        self.assertEqual((used['used_tokens'], used['used_credit']), (0, 0),
                         '读账单不能计成消耗')

    def test_blocked_ip_request_is_logged(self) -> None:
        token, _ = self._key(quota_credit=1)
        db.set_setting('security', {'enabled': True, 'mode': 'blacklist'})
        db.execute('INSERT INTO ip_rules(kind, cidr, note, created_at) '
                   "VALUES('deny', ?, '', ?)", ('127.0.0.1/32', int(time.time())))
        self._get('/v1/dashboard/billing/subscription', token)
        rows = db.query('SELECT status FROM request_logs')
        self.assertEqual([r['status'] for r in rows], [403])


class SettingsApiTest(_Base):
    def test_defaults(self) -> None:
        c = self._login('guest', 'guest-pw')
        r = c.get('/api/settings/billing')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {'billing_credit_price_usd': 0.03,
                                    'billing_usd_rate': 7.15,
                                    'billing_currency': 'usd'})

    def test_viewer_can_read_but_not_write(self) -> None:
        c = self._login('guest', 'guest-pw')
        r = c.post('/api/settings/billing', json={'billing_currency': 'cny'})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(db.get_setting('billing_currency', 'usd'), 'usd')

    def test_admin_session_writes_and_audits(self) -> None:
        c = self._login('admin', 'admin-pw')
        r = c.post('/api/settings/billing',
                   json={'billing_credit_price_usd': 0.05,
                         'billing_usd_rate': 7.2,
                         'billing_currency': 'cny'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {'billing_credit_price_usd': 0.05,
                                    'billing_usd_rate': 7.2,
                                    'billing_currency': 'cny'})
        self.assertEqual(db.get_setting('billing_credit_price_usd'), 0.05)
        self.assertEqual(db.get_setting('billing_usd_rate'), 7.2)
        self.assertEqual(db.get_setting('billing_currency'), 'cny')
        self.assertEqual(len(db.query("SELECT id FROM audit_logs "
                                      "WHERE action = 'update_billing_settings'")), 1,
                         '每次写入必须留一条审计')

    def test_partial_update_keeps_other_values(self) -> None:
        c = self._login('admin', 'admin-pw')
        c.post('/api/settings/billing', json={'billing_currency': 'cny'})
        self.assertEqual(db.get_setting('billing_credit_price_usd', 0.03), 0.03)
        self.assertEqual(db.get_setting('billing_usd_rate', 7.15), 7.15)

    def test_api_token_cannot_write(self) -> None:
        c = self._login('admin', 'admin-pw')
        token = c.post('/api/tokens', json={'name': 'ci', 'scope': 'admin'}).json()['token']
        r = self._client().post('/api/settings/billing',
                                json={'billing_currency': 'cny'},
                                headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 403, r.text)

    def test_invalid_values_are_400_and_write_nothing(self) -> None:
        c = self._login('admin', 'admin-pw')
        for bad in ({}, {'billing_currency': 'eur'}, {'billing_currency': 'CNY '},
                    {'billing_credit_price_usd': -1},
                    {'billing_credit_price_usd': 'x'},
                    {'billing_credit_price_usd': True},
                    {'billing_usd_rate': 0},
                    {'billing_usd_rate': -1}):
            with self.subTest(body=bad):
                r = c.post('/api/settings/billing', json=bad)
                self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(db.get_setting('billing_currency', 'usd'), 'usd')

    def test_anonymous_cannot_read(self) -> None:
        self.assertEqual(self._client().get('/api/settings/billing').status_code, 401)


class FlagInventoryTest(unittest.TestCase):
    def test_billing_defaults_match_contract_flag_inventory(self) -> None:
        for name, value in billing.DEFAULTS.items():
            self.assertEqual(settings_defaults.FLAG_DEFAULTS[name], value,
                             f'{name} 与契约 §5 的默认值不一致')


if __name__ == '__main__':
    unittest.main()
