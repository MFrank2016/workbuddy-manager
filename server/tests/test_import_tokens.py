"""批量 refresh token 导入：有界、逐条隔离、强制刷新、保留 profile。

这个端点与 `/api/accounts/upload` 的**根本区别**：upload 信任客户端给的
accessToken（离线拷贝即可用），import-tokens 只信 refreshToken——每条都当场
refresh，拿回来的 token 才落盘。因此这里钉住的是：

  1. 客户端 accessToken / expires_at 一律忽略，落盘值全部来自 refresh 响应；
  2. 嵌套 `{account, auth}` 与扁平条目都认，嵌套同名键优先；
  3. realm 优先级 `body.realm > auth.realm > domain 推导`；
  4. 每条失败互不牵连（缺 uid → rejected，refresh 失败 → failed，其余照常落盘）；
  5. 上限 200、并发 2、逐条 30s、整体 120s（超时未开始的记 failed/timeout）；
  6. 覆盖导入保留 `profile` / `profile_derived` / 未知顶层键；
  7. 审计只记数量，绝不出现 token。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config, db, security, upstreamsvc
from server.routers import accounts
from server.services import tencent, tokenimport


def _entry(uid: str, refresh: str, **extra) -> dict:
    return {'uid': uid, 'refreshToken': refresh, **extra}


class _ImportCase(unittest.TestCase):
    """临时 auth 目录 + 临时库 + 管理员会话（依赖覆盖）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.auth_dir = self.root / 'auths'
        self.auth_dir.mkdir()
        self._orig = (config.AUTH_DIR, config.DB_PATH, config.USERS_FILE,
                      config.AUTH_FILE_MODE)
        config.AUTH_DIR = self.auth_dir
        config.DB_PATH = self.root / 'manager.db'
        config.USERS_FILE = self.root / 'users.json'
        db._conn = None
        db.connect()
        self.addCleanup(self._close_db)

        self.app = FastAPI()
        self.app.include_router(accounts.router)
        self.app.dependency_overrides[security.current_user] = self._admin
        self.client = TestClient(self.app)

        # refresh 默认成功；具体用例按需替换。
        self.refresh_calls: list[dict] = []
        self.refresh_patch = mock.patch.object(
            tencent, 'refresh_token', side_effect=self._refresh_ok)
        self.refresh_patch.start()
        self.addCleanup(self.refresh_patch.stop)
        self.reload_patch = mock.patch.object(accounts.reload,
                                              'request_reload_or_restart')
        self.reload = self.reload_patch.start()
        self.addCleanup(self.reload_patch.stop)

    def _admin(self) -> dict:
        return {'username': 'admin', 'role': 'admin'}

    def _close_db(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        (config.AUTH_DIR, config.DB_PATH, config.USERS_FILE,
         config.AUTH_FILE_MODE) = self._orig

    async def _refresh_ok(self, auth: dict):
        self.refresh_calls.append(dict(auth))
        token = str(auth.get('refresh_token') or '')
        return True, '刷新成功', {
            'access_token': 'AT-' + token,
            'expires_at': 1900000000,
        }

    def _post(self, **body) -> dict:
        response = self.client.post('/api/accounts/import-tokens', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _saved(self, uid: str, directory: Path | None = None) -> dict:
        path = (directory or self.auth_dir) / f'workbuddy-{uid}.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def _audit_rows(self) -> list[dict]:
        return db.list_audit_logs(limit=50)


# ── 成功路径 ────────────────────────────────────────────────
class ImportSuccessTest(_ImportCase):
    def test_imports_two_entries_and_writes_both_files(self) -> None:
        body = self._post(realm='cn', entries=[
            _entry('u1', 'rt-1'),
            _entry('u2', 'rt-2'),
        ])
        self.assertEqual(body['total'], 2)
        self.assertEqual([r['status'] for r in body['results']], ['added', 'added'])
        self.assertEqual(body['counts']['added'], 2)
        self.assertEqual(self._saved('u1')['auth']['accessToken'], 'AT-rt-1')
        self.assertEqual(self._saved('u2')['auth']['accessToken'], 'AT-rt-2')
        self.assertEqual(self.reload.call_count, 2)

    def test_expires_at_and_access_token_come_from_refresh_not_client(self) -> None:
        async def refresh(auth: dict):
            self.refresh_calls.append(dict(auth))
            return True, '刷新成功', {'access_token': 'SERVER-AT',
                                      'expires_at': 1999999999}

        with mock.patch.object(tencent, 'refresh_token', side_effect=refresh):
            self._post(realm='cn', entries=[{
                'uid': 'u1', 'refreshToken': 'rt-1',
                # 客户端提供的一切 token/到期值都必须被忽略
                'accessToken': 'CLIENT-AT', 'access_token': 'CLIENT-AT',
                'expires_at': 111, 'expiresAt': 111,
            }])
        saved = self._saved('u1')
        self.assertEqual(saved['auth']['accessToken'], 'SERVER-AT')
        self.assertEqual(saved['auth']['expiresAt'], 1999999999)
        self.assertEqual(self.refresh_calls[0]['access_token'], '',
                         '客户端 accessToken 不应被拿去刷新')

    def test_nested_and_flat_entries_both_import(self) -> None:
        nested = {
            'account': {'uid': 'nested-1', 'nickname': '嵌套号'},
            'auth': {'accessToken': 'ignored', 'refreshToken': 'rt-nested',
                     'domain': 'copilot.tencent.com', 'realm': 'cn'},
        }
        flat = {'uid': 'flat-1', 'refresh_token': 'rt-flat', 'nickname': '扁平号'}
        body = self._post(realm='cn', entries=[nested, flat])
        self.assertEqual([r['status'] for r in body['results']], ['added', 'added'])
        self.assertEqual(self._saved('nested-1')['account']['nickname'], '嵌套号')
        self.assertEqual(self._saved('flat-1')['account']['nickname'], '扁平号')
        self.assertEqual(self._saved('flat-1')['auth']['accessToken'], 'AT-rt-flat')

    def test_nested_fields_win_over_same_named_flat_fields(self) -> None:
        body = self._post(realm='cn', entries=[{
            'uid': 'flat-uid',
            'refreshToken': 'rt-flat',
            'account': {'uid': 'nested-uid'},
            'auth': {'refreshToken': 'rt-nested'},
        }])
        self.assertEqual(body['results'][0]['uid'], 'nested-uid')
        self.assertEqual(self.refresh_calls[0]['refresh_token'], 'rt-nested')
        self.assertTrue((self.auth_dir / 'workbuddy-nested-uid.json').exists())
        self.assertFalse((self.auth_dir / 'workbuddy-flat-uid.json').exists())

    def test_camel_case_mapping(self) -> None:
        self._post(realm='cn', entries=[{
            'account': {'uid': 'camel-1', 'enterpriseId': 'ent-1'},
            'auth': {'accessToken': 'x', 'refreshToken': 'rt-camel'},
        }])
        saved = self._saved('camel-1')
        self.assertEqual(saved['account']['uid'], 'camel-1')
        self.assertEqual(saved['account']['enterpriseId'], 'ent-1')
        self.assertEqual(saved['auth']['refreshToken'], 'rt-camel')


# ── realm 优先级 ────────────────────────────────────────────
class ImportRealmTest(_ImportCase):
    def test_body_realm_wins_over_auth_realm_and_domain(self) -> None:
        self._post(realm='global', entries=[{
            'account': {'uid': 'r1'},
            'auth': {'refreshToken': 'rt', 'realm': 'cn',
                     'domain': 'copilot.tencent.com'},
        }])
        self.assertEqual(self._saved('r1')['auth']['realm'], 'global')
        self.assertEqual(self.refresh_calls[0]['realm'], 'global')

    def test_auth_realm_wins_over_domain(self) -> None:
        self._post(entries=[{
            'account': {'uid': 'r2'},
            'auth': {'refreshToken': 'rt', 'realm': 'global',
                     'domain': 'copilot.tencent.com'},
        }])
        self.assertEqual(self._saved('r2')['auth']['realm'], 'global')

    def test_missing_realm_falls_back_to_domain_then_cn(self) -> None:
        self._post(entries=[
            {'uid': 'r3', 'refreshToken': 'rt', 'domain': 'www.workbuddy.ai'},
            {'uid': 'r4', 'refreshToken': 'rt'},
        ])
        self.assertEqual(self._saved('r3')['auth']['realm'], 'global')
        self.assertEqual(self._saved('r4')['auth']['realm'], 'cn')

    def test_invalid_body_realm_is_400(self) -> None:
        response = self.client.post('/api/accounts/import-tokens',
                                    json={'realm': 'us', 'entries': [_entry('x', 'rt')]})
        self.assertEqual(response.status_code, 400)


# ── 逐条校验与隔离 ──────────────────────────────────────────
class ImportEntryValidationTest(_ImportCase):
    def test_missing_uid_is_rejected_without_writing(self) -> None:
        body = self._post(realm='cn', entries=[{'refreshToken': 'rt-1'}])
        self.assertEqual(body['results'][0]['status'], 'rejected')
        self.assertEqual(body['results'][0]['reason'], 'missing_uid')
        self.assertEqual(list(self.auth_dir.glob('*')), [])

    def test_missing_refresh_token_is_rejected(self) -> None:
        body = self._post(realm='cn', entries=[{'uid': 'u1'}])
        self.assertEqual(body['results'][0]['status'], 'rejected')
        self.assertEqual(body['results'][0]['reason'], 'missing_refresh_token')

    def test_path_like_uid_is_rejected(self) -> None:
        body = self._post(realm='cn', entries=[_entry('../evil', 'rt')])
        self.assertEqual(body['results'][0]['status'], 'rejected')
        self.assertEqual(body['results'][0]['reason'], 'invalid_uid')
        self.assertFalse((self.auth_dir.parent / 'evil.json').exists())

    def test_bad_entries_do_not_block_good_ones(self) -> None:
        async def refresh(auth: dict):
            if auth['refresh_token'] == 'rt-bad':
                return False, '刷新失败（HTTP 401）', {}
            return True, '刷新成功', {'access_token': 'AT-' + auth['refresh_token'],
                                      'expires_at': 1900000000}

        with mock.patch.object(tencent, 'refresh_token', side_effect=refresh):
            body = self._post(realm='cn', entries=[
                {'refreshToken': 'rt-1'},
                _entry('bad-refresh', 'rt-bad'),
                _entry('good', 'rt-good'),
            ])
        self.assertEqual([r['status'] for r in body['results']],
                         ['rejected', 'failed', 'added'])
        self.assertEqual(body['results'][1]['reason'], 'refresh_failed')
        self.assertTrue((self.auth_dir / 'workbuddy-good.json').exists())
        self.assertFalse((self.auth_dir / 'workbuddy-bad-refresh.json').exists())

    def test_write_failure_is_failed_not_added(self) -> None:
        with mock.patch.object(tencent, 'write_auth_file',
                               side_effect=OSError('disk full')):
            body = self._post(realm='cn', entries=[_entry('u1', 'rt-1')])
        self.assertEqual(body['results'][0]['status'], 'failed')
        self.assertEqual(body['results'][0]['reason'], 'write_failed')
        self.assertNotIn('disk full', json.dumps(body))

    def test_entry_error_message_never_contains_tokens(self) -> None:
        async def refresh(auth: dict):
            return False, '刷新失败（HTTP 401）', {}

        with mock.patch.object(tencent, 'refresh_token', side_effect=refresh):
            body = self._post(realm='cn', entries=[_entry('u1', 'rt-SECRET-1')])
        self.assertNotIn('rt-SECRET-1', json.dumps(body))


# ── 重复 / 覆盖 ────────────────────────────────────────────
class ImportDuplicateTest(_ImportCase):
    def test_existing_uid_without_overwrite_is_skipped(self) -> None:
        (self.auth_dir / 'workbuddy-u1.json').write_text(json.dumps({
            'account': {'uid': 'u1', 'nickname': '旧号'},
            'auth': {'accessToken': 'OLD', 'refreshToken': 'old-rt',
                     'expiresAt': 1, 'domain': 'copilot.tencent.com', 'realm': 'cn'},
        }), encoding='utf-8')
        body = self._post(realm='cn', entries=[_entry('u1', 'rt-new')])
        self.assertEqual(body['results'][0]['status'], 'skipped')
        self.assertEqual(body['results'][0]['reason'], 'exists')
        self.assertEqual(self._saved('u1')['auth']['accessToken'], 'OLD')
        self.assertEqual(self.refresh_calls, [], '已存在的账号不该白刷一次')

    def test_existing_uid_with_overwrite_is_updated(self) -> None:
        (self.auth_dir / 'workbuddy-u1.json').write_text(json.dumps({
            'account': {'uid': 'u1', 'nickname': '旧号'},
            'auth': {'accessToken': 'OLD', 'refreshToken': 'old-rt',
                     'expiresAt': 1, 'domain': 'copilot.tencent.com', 'realm': 'cn'},
        }), encoding='utf-8')
        body = self._post(realm='cn', overwrite=True,
                          entries=[_entry('u1', 'rt-new')])
        self.assertEqual(body['results'][0]['status'], 'updated')
        self.assertEqual(self._saved('u1')['auth']['accessToken'], 'AT-rt-new')

    def test_overwrite_preserves_profile_derived_and_unknown_keys(self) -> None:
        (self.auth_dir / 'workbuddy-u1.json').write_text(json.dumps({
            'account': {'uid': 'u1', 'nickname': '旧号'},
            'auth': {'accessToken': 'OLD', 'refreshToken': 'old-rt',
                     'expiresAt': 1, 'domain': 'copilot.tencent.com', 'realm': 'cn'},
            'profile': 'cn-cli',
            'profile_derived': 'cn-work',
            'future_field': {'keep': 'me'},
        }), encoding='utf-8')
        self._post(realm='cn', overwrite=True,
                   entries=[_entry('u1', 'rt-new', profile='intl-cli')])
        saved = self._saved('u1')
        self.assertEqual(saved['profile'], 'intl-cli')
        self.assertEqual(saved['profile_derived'], 'cn-work')
        self.assertEqual(saved['future_field'], {'keep': 'me'})
        self.assertEqual(saved['auth']['accessToken'], 'AT-rt-new')

    def test_supplied_profile_is_persisted_on_new_account(self) -> None:
        self._post(realm='global', entries=[
            _entry('u1', 'rt-1', profile='intl-work')])
        self.assertEqual(self._saved('u1')['profile'], 'intl-work')

    def _write_existing(self, expires_at: int) -> Path:
        path = self.auth_dir / 'workbuddy-u1.json'
        path.write_text(json.dumps({
            'account': {'uid': 'u1', 'nickname': '旧号'},
            'auth': {'accessToken': 'OLD', 'refreshToken': 'old-rt',
                     'expiresAt': expires_at, 'domain': 'copilot.tencent.com',
                     'realm': 'cn'},
        }), encoding='utf-8')
        return path

    def test_overwrite_without_expires_in_preserves_known_expiry(self) -> None:
        """刷新响应没带 expiresIn 时，**保留**旧的到期时间。

        `tencent.refresh_token` 在缺省 expiresIn 时故意不返回 `expires_at`
        （拿不到就不要乱写）。落盘如果把它当 0，就会把一个已知的有效期改成
        「未知」，`renew._needs_renew` 随即永久跳过这个账号。
        """
        path = self._write_existing(1900000000)

        async def refresh(auth: dict):
            self.refresh_calls.append(dict(auth))
            return True, '刷新成功（上游未返回有效期，沿用原值）', {
                'access_token': 'AT-rt-new'}

        with mock.patch.object(tencent, 'refresh_token', side_effect=refresh):
            body = self._post(realm='cn', overwrite=True,
                              entries=[_entry('u1', 'rt-new')])
        self.assertEqual(body['results'][0]['status'], 'updated')
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['auth']['accessToken'], 'AT-rt-new')
        self.assertEqual(saved['auth']['expiresAt'], 1900000000,
                         '已知到期时间被 0 覆盖了（数据丢失）')

    def test_overwrite_with_expires_in_takes_the_new_value(self) -> None:
        """上游给了 expiresIn → 用新值（保留语义不能挡住正常更新）。"""
        path = self._write_existing(1900000000)

        async def refresh(auth: dict):
            self.refresh_calls.append(dict(auth))
            return True, '刷新成功', {'access_token': 'AT-rt-new',
                                      'expires_at': 2000000000}

        with mock.patch.object(tencent, 'refresh_token', side_effect=refresh):
            self._post(realm='cn', overwrite=True,
                       entries=[_entry('u1', 'rt-new')])
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['auth']['expiresAt'], 2000000000)

    def test_new_account_without_expires_in_stays_unknown_zero(self) -> None:
        """新账号 + 上游没给有效期 → 记 0（未知），与 `_needs_renew` 同口径。"""
        async def refresh(auth: dict):
            return True, '刷新成功（上游未返回有效期）', {'access_token': 'AT'}

        with mock.patch.object(tencent, 'refresh_token', side_effect=refresh):
            body = self._post(realm='cn', entries=[_entry('new-1', 'rt')])
        self.assertEqual(body['results'][0]['status'], 'added')
        saved = self._saved('new-1')
        # 0 = 「未知」的既定表示：上游没给有效期，不臆测（`renew._needs_renew`
        # 对 <=0 返回 False，把「未知」交给上游/人工处理）。
        self.assertEqual(saved['auth']['expiresAt'], 0)
        self.assertEqual(saved['auth']['accessToken'], 'AT')

    def test_preserved_expiry_still_enters_the_renew_window(self) -> None:
        """保留的正数到期时间仍能被 `renew._needs_renew` 认出来。

        这是 B2 的真实后果：0 会让 `_needs_renew` 返回 False，账号从此被
        自动续期永久跳过；保留后应当照常进入续期窗口。
        """
        from server.services import renew
        soon = int(time.time()) + 3600          # 1 小时后到期 → 在续期窗口内
        path = self._write_existing(soon)

        async def refresh(auth: dict):
            return True, '刷新成功（上游未返回有效期）', {'access_token': 'AT'}

        with mock.patch.object(tencent, 'refresh_token', side_effect=refresh):
            self._post(realm='cn', overwrite=True,
                       entries=[_entry('u1', 'rt-new')])
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['auth']['expiresAt'], soon)
        self.assertTrue(renew._needs_renew(saved['auth']['expiresAt']),
                        '保留后的到期时间没被续期调度认出来')


# ── 分组 / 目录 ────────────────────────────────────────────
class ImportUpstreamTest(_ImportCase):
    def _group(self, name: str, auth_dir: str, enabled: bool = True) -> int:
        row = upstreamsvc.create_upstream(
            name, 'http://127.0.0.1:7991', auth_dir=auth_dir, enabled=enabled)
        return int(row['id'])

    def test_upstream_id_uses_its_own_auth_dir(self) -> None:
        group_dir = self.root / 'group-auths'
        group_dir.mkdir()
        group_id = self._group('分组甲', str(group_dir))
        body = self._post(upstream_id=group_id, realm='cn',
                          entries=[_entry('u1', 'rt-1')])
        self.assertEqual(body['results'][0]['status'], 'added')
        self.assertTrue((group_dir / 'workbuddy-u1.json').exists())
        self.assertFalse((self.auth_dir / 'workbuddy-u1.json').exists())

    def test_unknown_upstream_is_404(self) -> None:
        response = self.client.post('/api/accounts/import-tokens',
                                    json={'upstream_id': 99999, 'realm': 'cn',
                                          'entries': [_entry('u1', 'rt-1')]})
        self.assertEqual(response.status_code, 404)

    def test_disabled_upstream_is_400(self) -> None:
        group_dir = self.root / 'disabled-auths'
        group_dir.mkdir()
        group_id = self._group('停用组', str(group_dir), enabled=False)
        response = self.client.post('/api/accounts/import-tokens',
                                    json={'upstream_id': group_id, 'realm': 'cn',
                                          'entries': [_entry('u1', 'rt-1')]})
        self.assertEqual(response.status_code, 400)

    def test_upstream_without_auth_dir_is_400_upstream_auth_dir_unset(self) -> None:
        group_id = self._group('只转发组', '')
        response = self.client.post('/api/accounts/import-tokens',
                                    json={'upstream_id': group_id, 'realm': 'cn',
                                          'entries': [_entry('u1', 'rt-1')]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], 'upstream_auth_dir_unset')
        self.assertEqual(self.refresh_calls, [])
        self.assertEqual(response.json()['detail'], 'upstream_auth_dir_unset')
        self.assertEqual(self.refresh_calls, [])

    def test_unusable_upstream_auth_dir_is_400_upstream_auth_dir_unset(self) -> None:
        # 目录路径上挡着一个普通文件 = 不可用。用文件而不是 chmod 0500：
        # root 对 0500 目录依然可写，权限位在 root 下测不出「不可写」。
        blocked = self.root / 'not-a-dir'
        blocked.write_text('x', encoding='utf-8')
        group_id = self._group('坏目录组', str(blocked))
        response = self.client.post('/api/accounts/import-tokens',
                                    json={'upstream_id': group_id, 'realm': 'cn',
                                          'entries': [_entry('u1', 'rt-1')]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], 'upstream_auth_dir_unset')
        self.assertEqual(self.refresh_calls, [])

    def test_omitted_upstream_id_falls_back_to_config_auth_dir(self) -> None:
        body = self._post(realm='cn', entries=[_entry('u1', 'rt-1')])
        self.assertEqual(body['results'][0]['status'], 'added')
        self.assertTrue((self.auth_dir / 'workbuddy-u1.json').exists())


# ── 权限 / 并发 ────────────────────────────────────────────
class ImportGuardTest(_ImportCase):
    def test_viewer_gets_403(self) -> None:
        self.app.dependency_overrides[security.current_user] = lambda: {
            'username': 'viewer', 'role': 'viewer'}
        response = self.client.post('/api/accounts/import-tokens',
                                    json={'realm': 'cn', 'entries': [_entry('u1', 'rt')]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(list(self.auth_dir.glob('*')), [])

    def test_admin_api_token_gets_403_and_writes_no_file(self) -> None:
        """管理员 API Token 也不行：这个接口会落盘上游凭据。

        与 `/api/accounts/upload` 同一条边界（`require_session_admin`）：即便
        token 的 scope 是 admin，泄露后也不该能往号池里塞凭据。
        """
        from server import tokensvc
        created = tokensvc.create_token('probe-admin', 'admin')
        # 走真实的 current_user（会话 Cookie 优先、Bearer token 次之）。
        del self.app.dependency_overrides[security.current_user]
        self.addCleanup(lambda: self.app.dependency_overrides.__setitem__(
            security.current_user, self._admin))
        response = self.client.post('/api/accounts/import-tokens',
            json={'realm': 'cn', 'entries': [_entry('u1', 'rt')]},
            headers={'Authorization': f"Bearer {created['token']}"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.refresh_calls, [], '被拒绝的请求不该打腾讯')
        self.assertEqual(list(self.auth_dir.glob('*')), [], '403 路径不得落盘凭据')

    def test_session_admin_is_allowed(self) -> None:
        """会话管理员照旧可用（收紧不能误伤真人操作）。"""
        body = self._post(realm='cn', entries=[_entry('u1', 'rt')])
        self.assertEqual(body['counts']['added'], 1)

    def test_concurrent_import_returns_409(self) -> None:
        started = threading.Event()
        release = threading.Event()

        async def slow_refresh(auth: dict):
            started.set()
            await asyncio.to_thread(release.wait, 5)
            return True, '刷新成功', {'access_token': 'AT', 'expires_at': 1900000000}

        with mock.patch.object(tencent, 'refresh_token', side_effect=slow_refresh):
            first: list = []
            worker = threading.Thread(target=lambda: first.append(
                self.client.post('/api/accounts/import-tokens',
                                 json={'realm': 'cn',
                                       'entries': [_entry('slow', 'rt')]})))
            worker.start()
            self.assertTrue(started.wait(5), '首个导入没有开始')
            second = self.client.post('/api/accounts/import-tokens',
                                      json={'realm': 'cn', 'entries': [_entry('u2', 'rt')]})
            release.set()
            worker.join(10)
        self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(first[0].status_code, 200, first[0].text)
        # 首个导入结束后标记必须复位，否则面板会永久 409
        self.assertEqual(self._post(realm='cn', entries=[_entry('u3', 'rt')])
                         ['results'][0]['status'], 'added')


# ── 边界：数量 / 并发 / 超时 ────────────────────────────────
class ImportBoundsTest(_ImportCase):
    def test_more_than_200_entries_is_rejected(self) -> None:
        entries = [_entry(f'u{i}', f'rt-{i}') for i in range(201)]
        response = self.client.post('/api/accounts/import-tokens',
                                    json={'realm': 'cn', 'entries': entries})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.refresh_calls, [])
        self.assertEqual(list(self.auth_dir.glob('*')), [])

    def test_200_entries_is_accepted(self) -> None:
        entries = [_entry(f'u{i}', f'rt-{i}') for i in range(200)]
        body = self._post(realm='cn', entries=entries)
        self.assertEqual(body['total'], 200)
        self.assertEqual(body['counts']['added'], 200)

    def test_concurrency_is_capped_at_two(self) -> None:
        active = 0
        peak = 0

        async def refresh(auth: dict):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.02)
            finally:
                active -= 1
            return True, '刷新成功', {'access_token': 'AT', 'expires_at': 1900000000}

        with mock.patch.object(tencent, 'refresh_token', side_effect=refresh):
            body = self._post(realm='cn',
                              entries=[_entry(f'u{i}', f'rt-{i}') for i in range(4)])
        self.assertEqual(body['counts']['added'], 4)
        self.assertEqual(peak, 2, f'并发峰值 {peak}，应为 2')

    def test_single_entry_timeout_is_failed_timeout(self) -> None:
        async def slow(auth: dict):
            await asyncio.sleep(0.2)
            return True, '刷新成功', {'access_token': 'AT', 'expires_at': 1900000000}

        with mock.patch.object(tencent, 'refresh_token', side_effect=slow), \
                mock.patch.object(tokenimport, 'ENTRY_TIMEOUT', 0.05):
            body = self._post(realm='cn', entries=[_entry('u1', 'rt')])
        self.assertEqual(body['results'][0]['status'], 'failed')
        self.assertEqual(body['results'][0]['reason'], 'timeout')
        self.assertFalse((self.auth_dir / 'workbuddy-u1.json').exists())

    def test_entries_that_never_start_are_failed_timeout(self) -> None:
        with mock.patch.object(tokenimport, 'DEADLINE', -1.0):
            body = self._post(realm='cn', entries=[
                _entry('u1', 'rt-1'), _entry('u2', 'rt-2')])
        self.assertEqual([r['status'] for r in body['results']], ['failed', 'failed'])
        self.assertEqual([r['reason'] for r in body['results']], ['timeout', 'timeout'])
        self.assertEqual(self.refresh_calls, [], '整体超时后不应再发起刷新')


# ── 写盘权限 ───────────────────────────────────────────────
class ImportFileModeTest(_ImportCase):
    @unittest.skipUnless(os.name == 'posix', '权限位只在 POSIX 有意义')
    def test_new_file_is_0644(self) -> None:
        self._post(realm='cn', entries=[_entry('u1', 'rt')])
        mode = (self.auth_dir / 'workbuddy-u1.json').stat().st_mode & 0o777
        self.assertEqual(mode, 0o644)

    @unittest.skipUnless(os.name == 'posix', '权限位只在 POSIX 有意义')
    def test_existing_file_mode_is_unchanged(self) -> None:
        path = self.auth_dir / 'workbuddy-u1.json'
        path.write_text(json.dumps({
            'account': {'uid': 'u1'},
            'auth': {'accessToken': 'OLD', 'refreshToken': 'old-rt',
                     'expiresAt': 1, 'realm': 'cn'},
        }), encoding='utf-8')
        os.chmod(path, 0o640)
        self._post(realm='cn', overwrite=True, entries=[_entry('u1', 'rt-new')])
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    @unittest.skipUnless(os.name == 'posix', '权限位只在 POSIX 有意义')
    def test_env_override_sets_new_file_mode(self) -> None:
        with mock.patch.object(config, 'AUTH_FILE_MODE', 0o600):
            self._post(realm='cn', entries=[_entry('u1', 'rt')])
        mode = (self.auth_dir / 'workbuddy-u1.json').stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    @unittest.skipUnless(os.name == 'posix', '权限位只在 POSIX 有意义')
    def test_auth_dir_is_created_0750(self) -> None:
        config.AUTH_DIR = self.root / 'fresh-auths'
        self._post(realm='cn', entries=[_entry('u1', 'rt')])
        mode = config.AUTH_DIR.stat().st_mode & 0o777
        self.assertEqual(mode, 0o750)


# ── 审计 ───────────────────────────────────────────────────
class ImportAuditTest(_ImportCase):
    def test_audit_records_counts_without_tokens(self) -> None:
        body = self._post(realm='cn', entries=[
            _entry('u1', 'rt-SECRET-A'), _entry('u2', 'rt-SECRET-B')])
        self.assertEqual(body['counts']['added'], 2)
        rows = self._audit_rows()
        self.assertTrue(rows, '没有写审计')
        row = rows[0]
        self.assertEqual(row['action'], 'accounts.import_tokens')
        self.assertIn('added=2', row['detail'])
        blob = json.dumps(rows, ensure_ascii=False)
        for secret in ('rt-SECRET-A', 'rt-SECRET-B', 'AT-rt-SECRET-A'):
            self.assertNotIn(secret, blob)
        self.assertNotIn('SECRET', blob)


class WriteAuthFilePreservesTest(unittest.TestCase):
    """`write_auth_file` 从现有 JSON 起底：写 profile、保留派生值与未知键。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.auth_dir = Path(self._tmp.name)
        self.path = self.auth_dir / 'workbuddy-u1.json'
        self.path.write_text(json.dumps({
            'account': {'uid': 'u1', 'nickname': 'old', 'custom': 'keep'},
            'auth': {'accessToken': 'OLD', 'refreshToken': 'old-rt',
                     'expiresAt': 1, 'domain': 'copilot.tencent.com',
                     'realm': 'cn', 'extra': 'keep'},
            'profile': 'cn-cli',
            'profile_derived': 'cn-work',
            'future_field': {'keep': 'me'},
        }), encoding='utf-8')

    def _write(self, **extra) -> dict:
        account = {
            'uid': 'u1', 'access_token': 'NEW', 'refresh_token': 'new-rt',
            'expires_at': 2, 'domain': 'copilot.tencent.com', 'realm': 'cn',
            **extra,
        }
        tencent.write_auth_file(account, self.auth_dir)
        return json.loads(self.path.read_text(encoding='utf-8'))

    def test_writes_supplied_profile(self) -> None:
        saved = self._write(profile='intl-cli')
        self.assertEqual(saved['profile'], 'intl-cli')

    def test_preserves_profile_when_not_supplied(self) -> None:
        saved = self._write()
        self.assertEqual(saved['profile'], 'cn-cli')
        self.assertEqual(saved['profile_derived'], 'cn-work')

    def test_preserves_unknown_top_level_and_auth_keys(self) -> None:
        saved = self._write(profile='intl-work')
        self.assertEqual(saved['future_field'], {'keep': 'me'})
        self.assertEqual(saved['account']['custom'], 'keep')
        self.assertEqual(saved['auth']['extra'], 'keep')

    def test_known_fields_are_overwritten(self) -> None:
        saved = self._write()
        self.assertEqual(saved['auth']['accessToken'], 'NEW')
        self.assertEqual(saved['auth']['refreshToken'], 'new-rt')
        self.assertEqual(saved['auth']['expiresAt'], 2)

    def test_empty_profile_is_not_written(self) -> None:
        saved = self._write(profile='')
        self.assertEqual(saved['profile'], 'cn-cli')


if __name__ == '__main__':
    unittest.main()
