"""账号列表的产品 profile 展示（迁移 Todo 23）。

钉住三件事：
  1. `resolve_profile` 与 core `auth.ResolveProfile` 同一条选择链；
  2. 只有 `profile_backfill_v1=true` 时 `profile_derived` 才可信；
  3. `list_auth_accounts` 对嵌套形 / 扁平形都能给出 effective_profile，
     且不会把 profile 写进 realm（两者正交）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import keyprofiles, wb2api  # noqa: E402


class ResolveProfileTest(unittest.TestCase):
    def test_explicit_wins(self) -> None:
        self.assertEqual(keyprofiles.resolve_profile('intl-cli', 'cn-work', 'cn', ''), 'intl-cli')

    def test_unknown_explicit_is_ignored(self) -> None:
        self.assertEqual(keyprofiles.resolve_profile('bogus', '', 'cn', ''), 'cn-cli')

    def test_derived_used_only_when_passed(self) -> None:
        # 调用方只在 backfill_v1=true 时传 derived；函数本身不再判断。
        self.assertEqual(keyprofiles.resolve_profile('', 'intl-work', 'cn', ''), 'intl-work')

    def test_stored_realm_then_domain(self) -> None:
        self.assertEqual(keyprofiles.resolve_profile('', '', 'global', ''), 'intl-work')
        self.assertEqual(keyprofiles.resolve_profile('', '', 'cn', 'www.workbuddy.ai'), 'cn-cli')
        self.assertEqual(keyprofiles.resolve_profile('', '', '', 'www.codebuddy.ai'), 'intl-cli')
        self.assertEqual(keyprofiles.resolve_profile('', '', '', 'unknown.example'), 'cn-cli')


class ProfileFieldsTest(unittest.TestCase):
    def test_nested_shape(self) -> None:
        raw = {'profile': 'intl-cli', 'profile_derived': 'cn-work',
               'profile_backfill_v1': True}
        auth = {'realm': 'cn', 'domain': 'www.codebuddy.ai'}
        got = wb2api._profile_fields(raw, auth)
        self.assertEqual(got['profile'], 'intl-cli')
        self.assertEqual(got['profile_derived'], 'cn-work')
        self.assertTrue(got['profile_backfill_v1'])
        self.assertEqual(got['effective_profile'], 'intl-cli')
        self.assertEqual(got['profile_realm'], 'global')

    def test_flat_shape_and_untrusted_derived(self) -> None:
        raw = {}
        auth = {'profile_derived': 'intl-work', 'realm': 'cn', 'domain': 'www.workbuddy.cn'}
        got = wb2api._profile_fields(raw, auth)
        self.assertEqual(got['profile'], '')
        self.assertEqual(got['profile_derived'], 'intl-work')
        self.assertFalse(got['profile_backfill_v1'])
        self.assertEqual(got['effective_profile'], 'cn-cli', '不可信 derived 不能压过 realm；realm 优先于 domain')

    def test_backfill_flag_makes_derived_effective(self) -> None:
        raw = {'profile_derived': 'intl-work', 'profile_backfill_v1': True}
        got = wb2api._profile_fields(raw, {'realm': 'cn'})
        self.assertEqual(got['effective_profile'], 'intl-work')


class ListAuthAccountsProfileTest(unittest.TestCase):
    def test_list_includes_effective_profile(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'workbuddy-u1.json').write_text(json.dumps({
                'account': {'uid': 'u1', 'nickname': 'n1'},
                'auth': {'accessToken': 'at', 'expiresAt': 9999999999,
                         'realm': 'cn', 'domain': 'www.codebuddy.ai'},
                'profile': 'intl-cli',
            }), encoding='utf-8')
            rows = wb2api.list_auth_accounts(root)
            self.assertEqual(len(rows), 1)
            a = rows[0]
            self.assertEqual(a['effective_profile'], 'intl-cli')
            self.assertEqual(a['profile_realm'], 'global')
            # profile 不写进 realm：realm 仍按磁盘/域名口径
            self.assertEqual(a['realm'], 'cn')


if __name__ == '__main__':
    unittest.main()
