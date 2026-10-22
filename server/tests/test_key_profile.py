"""密钥的 profile 选择器（迁移 Todo 20）。

## 为什么 profile 与 realm 必须一致

realm 决定「国内版 / 国际版」账号池，profile 决定「CLI / 工作台」端点族。
`cn-*` 的账号只在 cn 池里，`intl-*` 只在 global 池里（契约 §1 的 base 表）。
于是 `profile=intl-cli, realm=cn` 不是「更细的限制」，而是**自相矛盾**：
core 按 profile 拼出 `www.codebuddy.ai` 的 URL，却按 realm 只从 cn 池选号 ——
结果是每次请求都 502/空池，而界面上看不出哪里配错了。所以写入前就拒掉。

## 为什么要读绑定上游的 api_keys[].profiles

manager 的 `api_keys.profile` 是**单值**，core 的 `api_keys[].profiles` 是
允许集。manager 允许一个 core 那侧不允许的 profile，症状同样是「保存成功、
调用被 400 profile_not_allowed」。子集校验把这类不一致挡在保存时。

**读不到就跳过**（不阻断保存）：上游不可达 / 该 key 不在上游配置里 /
未绑定 —— 这些都不代表配置非法，只是「此刻没法验证」。跳过时记 warning，
让运维在日志里看得到；宁可少校验一次，也不要把保存整个卡死。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException  # noqa: E402

from server import config, db, keysvc  # noqa: E402
from server.services import keyprofiles  # noqa: E402


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig


class ProfileMigrationTest(_DbCase):
    """迁移：新库与旧库都必须有 profile 列，默认 ''（= 不限）。"""

    def test_fresh_db_has_profile_column(self) -> None:
        cols = {r['name']: r for r in db.query('PRAGMA table_info(api_keys)')}
        self.assertIn('profile', cols)
        self.assertEqual(cols['profile']['dflt_value'], "''")
        self.assertEqual(cols['profile']['notnull'], 1)

    def test_migration_entry_present(self) -> None:
        entries = [e for e in db._MIGRATIONS if e[0] == 'api_keys' and e[1] == 'profile']
        self.assertEqual(len(entries), 1, 'api_keys.profile 必须在 _MIGRATIONS 里')
        self.assertIn("TEXT NOT NULL DEFAULT ''", entries[0][2])

    def test_old_db_gets_profile_column_and_keeps_rows(self) -> None:
        """存量库升级：加列后既有密钥仍可读，profile 为空 = 行为不变。"""
        path = Path(self._tmp.name) / 'old.db'
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute(
            'CREATE TABLE api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, '
            'key_hash TEXT NOT NULL, prefix TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, '
            'expires_at INTEGER, max_ips INTEGER NOT NULL DEFAULT 0, '
            "ip_allowlist TEXT NOT NULL DEFAULT '[]', models TEXT NOT NULL DEFAULT '[]', "
            "realm TEXT NOT NULL DEFAULT '', quota INTEGER NOT NULL DEFAULT 0, "
            'used_tokens INTEGER NOT NULL DEFAULT 0, quota_credit REAL NOT NULL DEFAULT 0, '
            'used_credit REAL NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, '
            'last_used_at INTEGER)')
        conn.execute("INSERT INTO api_keys(name, key_hash, prefix, created_at) "
                     "VALUES('old', 'h', 'wbk_old', 1)")
        conn.commit()
        conn.close()

        config.DB_PATH = path
        db._conn = None
        db.connect()
        cols = {r['name'] for r in db.query('PRAGMA table_info(api_keys)')}
        self.assertIn('profile', cols)
        row = db.query_one("SELECT name, profile FROM api_keys WHERE name = 'old'")
        self.assertEqual(row['profile'], '')
        self.assertEqual(keysvc.list_keys()[0]['profile'], '')


class KeyProfileRoundTripTest(_DbCase):
    """keysvc 的字段拷贝 / 持久化 / 校验。"""

    def test_create_key_persists_profile(self) -> None:
        made = keysvc.create_key('k', profile='intl-cli')
        self.assertEqual(made['profile'], 'intl-cli')
        row = keysvc.resolve(made['key'])
        self.assertEqual(row['profile'], 'intl-cli')
        self.assertEqual(keysvc.list_keys()[0]['profile'], 'intl-cli')

    def test_create_key_defaults_to_unrestricted(self) -> None:
        made = keysvc.create_key('k')
        self.assertEqual(made['profile'], '')
        self.assertEqual(keysvc.resolve(made['key'])['profile'], '')

    def test_invalid_profile_normalizes_to_unrestricted(self) -> None:
        """脏值吃掉成 ''（与 realm / upstream_id 同口径）：来自表单，格式问题
        不该让整次保存失败；'' 是安全方向吗？——见下一条测试的理由。"""
        made = keysvc.create_key('k', profile='bogus')
        self.assertEqual(made['profile'], '')

    def test_update_key_sets_and_clears_profile(self) -> None:
        made = keysvc.create_key('k')
        self.assertEqual(keysvc.update_key(made['id'], {'profile': 'cn-work'})['profile'], 'cn-work')
        # 显式 '' 是允许的：把密钥改回「不限」（与 realm 同口径，不能用真值判断）
        self.assertEqual(keysvc.update_key(made['id'], {'profile': ''})['profile'], '')

    def test_update_key_does_not_touch_profile_when_absent(self) -> None:
        made = keysvc.create_key('k', profile='cn-cli')
        updated = keysvc.update_key(made['id'], {'name': 'renamed'})
        self.assertEqual(updated['profile'], 'cn-cli', '未提交 profile 的 PATCH 不得清空它')


class ProfileNormalizationTest(unittest.TestCase):
    def test_all_four_profiles_accepted(self) -> None:
        for p in ('cn-cli', 'cn-work', 'intl-cli', 'intl-work'):
            with self.subTest(profile=p):
                self.assertEqual(keysvc._norm_profile(p), p)

    def test_whitespace_and_case_normalized(self) -> None:
        self.assertEqual(keysvc._norm_profile('  CN-CLI '), 'cn-cli')

    def test_unknown_values_are_unrestricted(self) -> None:
        for bad in ('', None, 'cn', 'desktop', 'intl-*', 3):
            with self.subTest(value=bad):
                self.assertEqual(keysvc._norm_profile(bad), '')


class RealmAgreementTest(unittest.TestCase):
    """profile 与 realm 必须一致：cn-* ↔ cn、intl-* ↔ global。"""

    def test_agreeing_pairs_pass(self) -> None:
        for profile, realm in (('cn-cli', 'cn'), ('cn-work', 'cn'),
                               ('intl-cli', 'global'), ('intl-work', 'global')):
            with self.subTest(profile=profile, realm=realm):
                self.assertIsNone(keyprofiles.realm_conflict(profile, realm))

    def test_conflicting_pairs_are_rejected(self) -> None:
        for profile, realm in (('intl-cli', 'cn'), ('cn-work', 'global'),
                               ('intl-work', 'cn'), ('cn-cli', 'global')):
            with self.subTest(profile=profile, realm=realm):
                msg = keyprofiles.realm_conflict(profile, realm)
                self.assertIsNotNone(msg, '必须报冲突')
                self.assertIn(profile, msg)
                self.assertIn(realm, msg)

    def test_empty_profile_or_realm_is_unrestricted(self) -> None:
        for profile, realm in (('', 'cn'), ('', ''), ('cn-cli', ''), ('intl-cli', '')):
            with self.subTest(profile=profile, realm=realm):
                self.assertIsNone(keyprofiles.realm_conflict(profile, realm))


class AllowedSetTest(unittest.TestCase):
    """从 core config 里取允许集：按 forward_api_key 匹配具体密钥条目。"""

    CFG = {
        'api_key': 'sk-main',
        'api_keys': [
            {'key': 'sk-a', 'profiles': ['cn-cli', 'intl-cli']},
            {'key': 'sk-b'},  # 省略 profiles = 不限
            {'key': 'sk-c', 'profiles': []},
            {'key': 'sk-d', 'profiles': ['*']},
        ],
    }

    def test_matches_entry_by_forward_key(self) -> None:
        self.assertEqual(keyprofiles.allowed_from_config(self.CFG, 'sk-a'),
                         {'cn-cli', 'intl-cli'})

    def test_missing_profiles_means_unrestricted(self) -> None:
        self.assertIsNone(keyprofiles.allowed_from_config(self.CFG, 'sk-b'))
        self.assertIsNone(keyprofiles.allowed_from_config(self.CFG, 'sk-c'))

    def test_wildcard_means_unrestricted(self) -> None:
        self.assertIsNone(keyprofiles.allowed_from_config(self.CFG, 'sk-d'))

    def test_no_matching_entry_returns_none(self) -> None:
        self.assertIsNone(keyprofiles.allowed_from_config(self.CFG, 'sk-nope'))

    def test_empty_key_or_config_returns_none(self) -> None:
        self.assertIsNone(keyprofiles.allowed_from_config(self.CFG, ''))
        self.assertIsNone(keyprofiles.allowed_from_config({}, 'sk-a'))
        self.assertIsNone(keyprofiles.allowed_from_config(None, 'sk-a'))


class SubsetCheckTest(unittest.TestCase):
    def test_subset_passes(self) -> None:
        self.assertIsNone(keyprofiles.subset_conflict('cn-cli', {'cn-cli', 'intl-cli'}))

    def test_not_subset_rejected(self) -> None:
        msg = keyprofiles.subset_conflict('intl-work', {'cn-cli', 'intl-cli'})
        self.assertIsNotNone(msg)
        self.assertIn('intl-work', msg)

    def test_unrestricted_core_allows_anything(self) -> None:
        self.assertIsNone(keyprofiles.subset_conflict('intl-work', None))

    def test_empty_manager_profile_is_unrestricted(self) -> None:
        self.assertIsNone(keyprofiles.subset_conflict('', {'cn-cli'}))


class _UpstreamCase(unittest.TestCase):
    """默认上游读 config.json；非默认走 panel API。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg_path = Path(self._tmp.name) / 'config.json'
        self.cfg_path.write_text(json.dumps(AllowedSetTest.CFG), encoding='utf-8')
        patcher = mock.patch.object(config, 'UPSTREAM_CONFIG', self.cfg_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_upstream_reads_config_file(self) -> None:
        # 默认上游的 forward key 来自 config.upstream_api_key()；这里指向 api_keys
        # 里的某个分组密钥，才能验证「按 key 精确匹配」。
        with mock.patch.object(config, 'upstream_api_key', return_value='sk-a'):
            allowed, warning = keyprofiles.fetch_allowed(keyprofiles.DEFAULT)
        self.assertEqual(allowed, {'cn-cli', 'intl-cli'})
        self.assertIsNone(warning)

    def test_main_key_is_unrestricted_without_noise(self) -> None:
        """主密钥（顶层 api_key）按定义不限 —— 不校验、也不刷 warning。

        契约 §1 的「无匹配 → 跳过 + warning」针对的是**分组密钥查不到条目**
        （那说明配置漂移）；主密钥本就不该出现在 api_keys[] 里，每次保存都记一条
        只会训练运维忽略这个日志。
        """
        with mock.patch.object(config, 'upstream_api_key', return_value='sk-main'):
            allowed, warning = keyprofiles.fetch_allowed(keyprofiles.DEFAULT)
        self.assertIsNone(allowed)
        self.assertIsNone(warning)

    def test_missing_config_file_skips_with_warning(self) -> None:
        self.cfg_path.unlink()
        with self.assertLogs('workbuddy.keys', level='WARNING'):
            allowed, warning = keyprofiles.fetch_allowed(keyprofiles.DEFAULT)
        self.assertIsNone(allowed)
        self.assertTrue(warning)

    def test_non_default_upstream_calls_panel_api(self) -> None:
        calls: list = []

        def fake_get(url, headers):
            calls.append((url, headers))
            return {'ok': True, 'config': {'api_keys': [
                {'key': 'group-key', 'profiles': ['intl-cli', 'intl-work']},
            ]}}

        upstream = {'id': 3, 'base_url': 'http://10.0.0.9:7863',
                    'api_key': 'group-key', 'enabled': True, 'is_default': False}
        with mock.patch.object(keyprofiles, '_get_json', fake_get):
            allowed, warning = keyprofiles.fetch_allowed(upstream)
        self.assertEqual(allowed, {'intl-cli', 'intl-work'})
        self.assertIsNone(warning)
        self.assertEqual(calls[0][0], 'http://10.0.0.9:7863/panel/api/config')
        self.assertEqual(calls[0][1].get('Authorization'), 'Bearer group-key')

    def test_unreachable_upstream_skips_with_warning(self) -> None:
        upstream = {'id': 3, 'base_url': 'http://10.0.0.9:7863',
                    'api_key': 'k', 'enabled': True, 'is_default': False}

        def boom(url, headers):
            raise OSError('connection refused')

        with mock.patch.object(keyprofiles, '_get_json', boom), \
                self.assertLogs('workbuddy.keys', level='WARNING'):
            allowed, warning = keyprofiles.fetch_allowed(upstream)
        self.assertIsNone(allowed)
        self.assertTrue(warning)

    def test_upstream_without_base_url_skips(self) -> None:
        upstream = {'id': 3, 'base_url': '', 'api_key': 'k',
                    'enabled': True, 'is_default': False}
        allowed, warning = keyprofiles.fetch_allowed(upstream)
        self.assertIsNone(allowed)
        self.assertTrue(warning)


class RouteValidationTest(_UpstreamCase):
    """keys.py 的写入前校验：realm 一致 + 子集；PATCH 用合并后的有效值。"""

    def _patch_route(self, key_id: int, body: dict, user: dict | None = None):
        from server.routers import keys as keys_router
        return keys_router.update_key(
            key_id, keys_router.KeyPatch(**body),
            _Request('9.9.9.9'), user or {'username': 'admin', 'role': 'admin'})

    def test_create_rejects_conflicting_realm(self) -> None:
        from server.routers import keys as keys_router
        with self.assertRaises(HTTPException) as ctx:
            keys_router.create_key(
                keys_router.KeyIn(name='k', realm='cn', profile='intl-cli'),
                _Request('9.9.9.9'), {'username': 'admin', 'role': 'admin'})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn('profile', str(ctx.exception.detail))

    def test_create_accepts_agreeing_profile(self) -> None:
        from server.routers import keys as keys_router
        out = keys_router.create_key(
            keys_router.KeyIn(name='k', realm='cn', profile='cn-cli'),
            _Request('9.9.9.9'), {'username': 'admin', 'role': 'admin'})
        self.assertEqual(out['profile'], 'cn-cli')

    def test_create_rejects_profile_outside_upstream_set(self) -> None:
        from server.routers import keys as keys_router
        with mock.patch.object(keyprofiles, 'fetch_allowed',
                               return_value=({'cn-cli'}, None)):
            with self.assertRaises(HTTPException) as ctx:
                keys_router.create_key(
                    keys_router.KeyIn(name='k', realm='global', profile='intl-work'),
                    _Request('9.9.9.9'), {'username': 'admin', 'role': 'admin'})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_patch_only_profile_validates_merged_realm(self) -> None:
        """存量密钥 realm=cn，只改 profile=intl-cli → 400（合并后的有效值冲突）。"""
        made = keysvc.create_key('k', realm='cn')
        with self.assertRaises(HTTPException) as ctx:
            self._patch_route(made['id'], {'profile': 'intl-cli'})
        self.assertEqual(ctx.exception.status_code, 400)
        # 未写入
        self.assertEqual(keysvc.resolve(made['key'])['profile'], '')

    def test_patch_only_profile_on_unrestricted_realm_is_allowed(self) -> None:
        made = keysvc.create_key('k', realm='')
        out = self._patch_route(made['id'], {'profile': 'intl-cli'})
        self.assertEqual(out['profile'], 'intl-cli')

    def test_patch_only_realm_validates_merged_profile(self) -> None:
        made = keysvc.create_key('k', profile='cn-cli')
        with self.assertRaises(HTTPException) as ctx:
            self._patch_route(made['id'], {'realm': 'global'})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_patch_clearing_profile_is_allowed(self) -> None:
        made = keysvc.create_key('k', realm='cn', profile='cn-cli')
        out = self._patch_route(made['id'], {'profile': ''})
        self.assertEqual(out['profile'], '')

    def test_patch_audits_profile_change_with_source_ip(self) -> None:
        """审计在 PATCH handler 里（它才有 request / 来源 IP）。"""
        made = keysvc.create_key('k', realm='cn')
        from server.routers import keys as keys_router
        with mock.patch.object(keys_router.security, 'audit') as audit:
            keys_router.update_key(
                made['id'], keys_router.KeyPatch(profile='cn-cli'),
                _Request('9.9.9.9'), {'username': 'admin', 'role': 'admin'})
        audit.assert_called_once()
        args = audit.call_args[0]
        self.assertEqual(args[1], 'update_key')
        detail = args[3]
        self.assertIn('cn-cli', detail)
        self.assertIn('9.9.9.9', detail, '审计必须带来源 IP')

    def test_patch_without_profile_change_still_audits(self) -> None:
        made = keysvc.create_key('k')
        from server.routers import keys as keys_router
        with mock.patch.object(keys_router.security, 'audit') as audit:
            keys_router.update_key(
                made['id'], keys_router.KeyPatch(name='new'),
                _Request('9.9.9.9'), {'username': 'admin', 'role': 'admin'})
        audit.assert_called_once()
        self.assertEqual(audit.call_args[0][1], 'update_key')

    def test_unreachable_upstream_does_not_block_save(self) -> None:
        from server.routers import keys as keys_router
        with mock.patch.object(keyprofiles, 'fetch_allowed',
                               return_value=(None, '上游不可达')):
            out = keys_router.create_key(
                keys_router.KeyIn(name='k', realm='global', profile='intl-work'),
                _Request('9.9.9.9'), {'username': 'admin', 'role': 'admin'})
        self.assertEqual(out['profile'], 'intl-work')

    def test_404_still_wins_over_profile_validation(self) -> None:
        """不存在的密钥：先 404，不要因为 profile 校验先报 400（会误导）。"""
        with self.assertRaises(HTTPException) as ctx:
            self._patch_route(9999, {'profile': 'cn-cli'})
        self.assertEqual(ctx.exception.status_code, 404)


class RestartNoticeTest(unittest.TestCase):
    """保存 profiles.* 必须提示重启（restart_required）。

    core 侧把 `profiles.enabled` / `profiles.detect_only` 放进 restart_paths；
    面板侧用保存前快照算 diff。这里钉住面板侧的判定：改了 profiles 的键会出现在
    需重启清单里，没改则不会。
    """

    def setUp(self) -> None:
        from server.routers import settings as S
        self.S = S

    def test_profiles_change_is_restart_required(self) -> None:
        prev = json.dumps({'profiles': {'enabled': False, 'detect_only': True}})
        cur = {'profiles': {'enabled': True, 'detect_only': True}}
        got = self.S._changed_restart_fields(
            prev, cur, ['profiles.enabled', 'profiles.detect_only'])
        self.assertEqual(got, ['profiles.enabled'])

    def test_detect_only_change_is_restart_required(self) -> None:
        prev = json.dumps({'profiles': {'enabled': False, 'detect_only': True}})
        cur = {'profiles': {'enabled': False, 'detect_only': False}}
        got = self.S._changed_restart_fields(
            prev, cur, ['profiles.enabled', 'profiles.detect_only'])
        self.assertEqual(got, ['profiles.detect_only'])

    def test_unchanged_profiles_yields_nothing(self) -> None:
        prev = json.dumps({'profiles': {'enabled': False, 'detect_only': True}})
        cur = {'profiles': {'enabled': False, 'detect_only': True}}
        self.assertEqual(self.S._changed_restart_fields(
            prev, cur, ['profiles.enabled', 'profiles.detect_only']), [])


class _Request:
    """最小 Request 替身：只提供 client_ip 需要的字段。"""

    def __init__(self, ip: str) -> None:
        self.headers: dict = {}
        self.client = _Client(ip)


class _Client:
    def __init__(self, host: str) -> None:
        self.host = host
