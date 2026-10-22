"""管理端并集目录与 `/api/settings/catalog` 端点（迁移 Todo 18）。

## 这个 flag 管的是什么

多上游部署里，每个分组（上游实例）有自己的模型清单。默认**不并集**：管理端
模型中心只展示默认分组的那份，`/v1/models` 只展示这把密钥所属分组的那份
（这是既有行为，也是 golden 钉住的行为）。打开 `catalog_group_merge` 后，
**仅管理端目录**变成所有分组的并集，并给每个模型标上 `available_in_groups`，
管理员据此知道「这个模型哪些组有」。

## 必须钉住的性质（做错了都是静默的）

  1. `catalog_group_merge=false` → `/v1/models` 与提交的 golden 逐字一致；
     flag 只影响管理端目录，**永不**让 `/v1/models` 跨组并集（否则下游客户端
     会看到自己调不了的模型，正是 issue #46 那类「能选、一选就失败」）。
  2. `true` → 管理端目录是并集，重复模型只出现一次，`available_in_groups`
     列出所有有它的分组（组 1 A/B、组 2 B/C → A/B/C，B = [1,2]）。
  3. 某个分组无 api_key 或超时 → **只降级这一组**，其余组照常返回，并把原因
     放进 `group_errors`；整个目录不能因为一个组挂了而变空。
  4. settings 端点固定为 `GET/POST /api/settings/catalog`：GET 任何登录用户可读
     （viewer 也要知道目录口径），POST 仅会话管理员（API Token 一律 403），
     每次写入留一条审计。billing 的 settings 不在这里（Todo 17 负责）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402

from server import config, db, upstreamsvc  # noqa: E402
from server.services import modelcatalog  # noqa: E402
from server.tests import golden_capture  # noqa: E402
from server.tests.catalog_group_fixtures import (  # noqa: E402
    ApiCase, FakeUpstreams, payload,
)

GOLDEN = Path(__file__).resolve().parent / 'golden'


class CatalogSettingsApiTest(ApiCase, unittest.TestCase):
    """`GET/POST /api/settings/catalog` 的权限、默认值与审计。"""

    def test_default_is_false(self) -> None:
        with self.as_user('guest', 'guest-pw') as c:
            r = c.get('/api/settings/catalog')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()['catalog_group_merge'], False,
                      '契约 §5：catalog_group_merge 默认 false')

    def test_viewer_can_read_but_not_write(self) -> None:
        with self.as_user('guest', 'guest-pw') as c:
            self.assertEqual(c.get('/api/settings/catalog').status_code, 200)
            r = c.post('/api/settings/catalog', json={'catalog_group_merge': True})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIs(db.get_setting('catalog_group_merge', False), False,
                      '被拒的写入不能改库')

    def test_admin_session_writes_and_audits(self) -> None:
        before = len(db.query('SELECT id FROM audit_logs'))
        with self.as_user('admin', 'admin-pw') as c:
            r = c.post('/api/settings/catalog', json={'catalog_group_merge': True})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIs(r.json()['catalog_group_merge'], True)
            self.assertIs(c.get('/api/settings/catalog').json()['catalog_group_merge'], True)
        self.assertIs(db.get_setting('catalog_group_merge', False), True)
        rows = db.query("SELECT action FROM audit_logs WHERE action = 'update_catalog_settings'")
        self.assertEqual(len(rows), 1, '每次写入必须留一条审计')
        self.assertGreater(len(db.query('SELECT id FROM audit_logs')), before)

    def test_api_token_cannot_write(self) -> None:
        token = self.admin_token()
        r = self.c.post('/api/settings/catalog', json={'catalog_group_merge': True},
                        headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn('API Token', r.json().get('detail', ''))
        self.assertIs(db.get_setting('catalog_group_merge', False), False)

    def test_invalid_payload_is_400_and_writes_nothing(self) -> None:
        for bad in ({}, {'catalog_group_merge': 'true'}, {'catalog_group_merge': 1},
                    {'catalog_group_merge': None}):
            with self.subTest(body=bad):
                with self.as_user('admin', 'admin-pw') as c:
                    r = c.post('/api/settings/catalog', json=bad)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIs(db.get_setting('catalog_group_merge', False), False)

    def test_anonymous_cannot_read(self) -> None:
        self.c.cookies.clear()
        self.assertEqual(self.c.get('/api/settings/catalog').status_code, 401)


class AdminCatalogMergeTest(ApiCase, unittest.TestCase):
    """flag off/on 的管理端目录差异，以及 `/v1/models` 永不跨组并集。"""

    def groups(self) -> tuple[dict, dict]:
        g1 = upstreamsvc.create_upstream(name='A 组', base_url='http://g1.invalid',
                                         api_key='k1')
        g2 = upstreamsvc.create_upstream(name='B 组', base_url='http://g2.invalid',
                                         api_key='k2')
        return g1, g2

    def test_flag_off_admin_catalog_has_no_availability_field(self) -> None:
        self.groups()
        fake = FakeUpstreams({
            'http://default.invalid': payload('a-model', 'b-model'),
            'http://g1.invalid': payload('a-model', 'b-model'),
            'http://g2.invalid': payload('b-model', 'c-model'),
        })
        with mock.patch.object(config, 'http_client', fake.client), \
                self.as_user('guest', 'guest-pw') as c:
            r = c.get('/api/model-catalog?realm=cn&force=true')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual([m['id'] for m in body['models']], ['a-model', 'b-model'],
                         'flag 关时只展示默认分组的清单')
        for m in body['models']:
            self.assertNotIn('available_in_groups', m)
        self.assertNotIn('groups', body)

    def test_flag_on_admin_catalog_is_the_union(self) -> None:
        g1, g2 = self.groups()
        fake = FakeUpstreams({
            'http://default.invalid': payload(),
            'http://g1.invalid': payload('a-model', 'b-model'),
            'http://g2.invalid': payload('b-model', 'c-model'),
        })
        with mock.patch.object(config, 'http_client', fake.client), \
                self.as_user('admin', 'admin-pw') as c:
            self.assertEqual(c.post('/api/settings/catalog',
                                    json={'catalog_group_merge': True}).status_code, 200)
            r = c.get('/api/model-catalog?realm=cn&force=true')
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        by_id = {m['id']: m for m in body['models']}
        self.assertEqual(set(by_id), {'a-model', 'b-model', 'c-model'},
                         'flag 开时管理端目录是各分组的并集')
        self.assertEqual(by_id['b-model']['available_in_groups'],
                         [g1['id'], g2['id']])
        self.assertEqual(by_id['a-model']['available_in_groups'], [g1['id']])
        self.assertEqual(by_id['c-model']['available_in_groups'], [g2['id']])

    def test_flag_on_one_group_timeout_still_returns_others(self) -> None:
        g1, _ = self.groups()
        fake = FakeUpstreams({
            'http://default.invalid': payload(),
            'http://g1.invalid': payload('a-model', 'b-model'),
            'http://g2.invalid': httpx.ConnectTimeout('timed out'),
        })
        with mock.patch.object(config, 'http_client', fake.client), \
                self.as_user('admin', 'admin-pw') as c:
            c.post('/api/settings/catalog', json={'catalog_group_merge': True})
            body = c.get('/api/model-catalog?realm=cn&force=true').json()
        by_id = {m['id']: m for m in body['models']}
        self.assertEqual(set(by_id), {'a-model', 'b-model'},
                         '组 2 超时，组 1 的模型仍要返回')
        self.assertEqual(by_id['b-model']['available_in_groups'], [g1['id']])
        self.assertTrue(body['group_errors'], '降级原因要作为 error 元数据返回')

    def test_v1_models_never_unions_across_groups(self) -> None:
        self.groups()
        fake = FakeUpstreams({
            'http://default.invalid': payload('default-model'),
            'http://g1.invalid': payload('a-model', 'b-model'),
            'http://g2.invalid': payload('b-model', 'c-model'),
        })
        token = self.client_key()
        with mock.patch.object(config, 'http_client', fake.client), \
                self.as_user('admin', 'admin-pw') as c:
            c.post('/api/settings/catalog', json={'catalog_group_merge': True})
            r = c.get('/v1/models', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 200, r.text)
        ids = [m['id'] for m in r.json()['data']]
        self.assertEqual(ids, ['default-model'],
                         '/v1/models 只给这把密钥所属分组（未绑定 = 默认组）的清单')
        for m in r.json()['data']:
            self.assertNotIn('available_in_groups', m,
                             '可用分组是管理端字段，绝不能下发给下游客户端')

    def test_flag_off_v1_models_matches_golden_byte_for_byte(self) -> None:
        """flag 关（默认）时 `/v1/models` 与提交的 golden 逐字一致。

        采集口径与 `golden_capture` 相同（`sort_keys` + 两空格缩进 + 末尾换行），
        否则「逐字一致」会因为序列化器不同而永远失败。
        """
        committed = (GOLDEN / 'models_listing.json').read_text(encoding='utf-8')
        fake = FakeUpstreams({
            'http://default.invalid': golden_capture.UPSTREAM_MODELS_PAYLOAD,
        })
        token = self.client_key()
        with mock.patch.object(config, 'http_client', fake.client):
            r = self.c.get('/v1/models', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(golden_capture._dumps(r.json()), committed)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
