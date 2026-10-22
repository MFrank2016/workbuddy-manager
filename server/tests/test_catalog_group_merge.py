"""按分组并集模型清单的服务层行为（迁移 Todo 18）。

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

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402

from server import config, db  # noqa: E402
from server.services import catalogmerge, modelcatalog  # noqa: E402
from server.tests.catalog_group_fixtures import (  # noqa: E402
    DbCase, FakeUpstreams, payload,
)


class GetModelsForUpstreamTest(unittest.TestCase):
    """`get_models_for_upstream(upstream)`：用**该组自己的**地址与钥匙拉清单。"""

    def _call(self, upstream: dict, routes: dict[str, object]):
        fake = FakeUpstreams(routes)
        with mock.patch.object(config, 'http_client', fake.client):
            out = asyncio.run(modelcatalog.get_models_for_upstream(upstream))
        return out, fake

    def test_uses_own_base_and_own_key(self) -> None:
        up = {'id': 2, 'name': 'B 组', 'base_url': 'http://group2.invalid',
              'api_key': 'group-2-key', 'enabled': True}
        out, fake = self._call(up, {'http://group2.invalid': payload('b-model')})
        self.assertEqual(out['models'][0]['id'], 'b-model')
        self.assertEqual(out['error'], '')
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]['base'], 'http://group2.invalid',
                         '必须打到该组自己的地址，而不是默认上游')
        self.assertEqual(fake.calls[0]['path'], '/v1/models')
        self.assertEqual(fake.calls[0]['auth'], 'Bearer group-2-key',
                         '必须用该组自己的 api_key')

    def test_missing_api_key_is_error_metadata_not_an_exception(self) -> None:
        up = {'id': 3, 'name': '空钥匙组', 'base_url': 'http://noauth.invalid',
              'api_key': '', 'enabled': True}
        out, fake = self._call(up, {'http://noauth.invalid': payload('x')})
        self.assertEqual(out['models'], [])
        self.assertTrue(out['error'], '无 api_key 必须返回原因')
        self.assertEqual(fake.calls, [], '没有钥匙就不该发请求')

    def test_timeout_is_error_metadata(self) -> None:
        up = {'id': 4, 'name': '慢组', 'base_url': 'http://slow.invalid',
              'api_key': 'k', 'enabled': True}
        out, _ = self._call(up, {'http://slow.invalid': httpx.ConnectTimeout('timed out')})
        self.assertEqual(out['models'], [])
        self.assertIn('timed out', out['error'].lower(),
                      '超时原因应可读（err_text 渲染成异常类型 + 原文）')

    def test_http_error_is_error_metadata(self) -> None:
        up = {'id': 5, 'name': '坏组', 'base_url': 'http://bad.invalid',
              'api_key': 'k', 'enabled': True}
        fake = FakeUpstreams({})          # 404
        with mock.patch.object(config, 'http_client', fake.client):
            out = asyncio.run(modelcatalog.get_models_for_upstream(up))
        self.assertEqual(out['models'], [])
        self.assertTrue(out['error'])

    def test_non_list_body_is_error_metadata(self) -> None:
        up = {'id': 6, 'name': '怪组', 'base_url': 'http://weird.invalid',
              'api_key': 'k', 'enabled': True}
        out, _ = self._call(up, {'http://weird.invalid': {'object': 'list'}})
        self.assertEqual(out['models'], [])
        self.assertTrue(out['error'])


class GroupAvailabilityTest(DbCase, unittest.TestCase):
    """并集与降级：两组不同模型、某组超时只降级它自己。"""

    def test_union_marks_shared_model_with_both_groups(self) -> None:
        g1 = self.group('A 组', 'http://g1.invalid')
        g2 = self.group('B 组', 'http://g2.invalid')
        fake = FakeUpstreams({
            'http://default.invalid': payload(),      # 默认组不参与这份 fixture
            'http://g1.invalid': payload('a-model', 'b-model'),
            'http://g2.invalid': payload('b-model', 'c-model'),
        })
        with mock.patch.object(config, 'http_client', fake.client):
            out = asyncio.run(catalogmerge.group_availability('cn'))
        merged = out['available_in_groups']
        self.assertEqual(set(merged), {'a-model', 'b-model', 'c-model'})
        self.assertEqual(merged['a-model'], [g1['id']])
        self.assertEqual(merged['b-model'], [g1['id'], g2['id']],
                         '两组都有的模型要同时标出两个分组')
        self.assertEqual(merged['c-model'], [g2['id']])
        self.assertEqual(out['errors'], [])

    def test_one_group_timeout_keeps_the_other(self) -> None:
        g1 = self.group('A 组', 'http://g1.invalid')
        self.group('B 组', 'http://g2.invalid')
        fake = FakeUpstreams({
            'http://default.invalid': payload(),
            'http://g1.invalid': payload('a-model', 'b-model'),
            'http://g2.invalid': httpx.ConnectTimeout('timed out'),
        })
        with mock.patch.object(config, 'http_client', fake.client):
            out = asyncio.run(catalogmerge.group_availability('cn'))
        self.assertEqual(set(out['available_in_groups']), {'a-model', 'b-model'},
                         '超时的那组降级，另一组仍要返回')
        self.assertEqual(out['available_in_groups']['b-model'], [g1['id']])
        self.assertTrue(any('B 组' in e for e in out['errors']),
                        f'错误元数据要指出是哪一组：{out["errors"]}')

    def test_group_without_key_degrades_without_request(self) -> None:
        g1 = self.group('A 组', 'http://g1.invalid')
        self.group('空组', 'http://nokey.invalid', key='')
        fake = FakeUpstreams({
            'http://default.invalid': payload(),
            'http://g1.invalid': payload('a-model'),
        })
        with mock.patch.object(config, 'http_client', fake.client):
            out = asyncio.run(catalogmerge.group_availability('cn'))
        self.assertEqual(out['available_in_groups'], {'a-model': [g1['id']]})
        self.assertTrue(any('空组' in e for e in out['errors']))
        self.assertNotIn('http://nokey.invalid', {c['base'] for c in fake.calls})

    def test_models_from_other_realm_are_not_merged(self) -> None:
        g1 = self.group('A 组', 'http://g1.invalid')
        fake = FakeUpstreams({
            'http://default.invalid': payload(),
            'http://g1.invalid': payload('cn-model', 'global:intl-model'),
        })
        with mock.patch.object(config, 'http_client', fake.client):
            cn = asyncio.run(catalogmerge.group_availability('cn'))
            modelcatalog.invalidate()
            glob = asyncio.run(catalogmerge.group_availability('global'))
        self.assertEqual(set(cn['available_in_groups']), {'cn-model'})
        self.assertEqual(set(glob['available_in_groups']), {'intl-model'})
        self.assertEqual(glob['available_in_groups']['intl-model'], [g1['id']])

    def test_disabled_group_is_skipped(self) -> None:
        self.group('A 组', 'http://g1.invalid')
        g2 = self.group('停用组', 'http://g2.invalid')
        from server import upstreamsvc
        upstreamsvc.update_upstream(g2['id'], {'enabled': False})
        fake = FakeUpstreams({
            'http://default.invalid': payload(),
            'http://g1.invalid': payload('a-model'),
            'http://g2.invalid': payload('c-model'),
        })
        with mock.patch.object(config, 'http_client', fake.client):
            out = asyncio.run(catalogmerge.group_availability('cn'))
        self.assertEqual(set(out['available_in_groups']), {'a-model'},
                         '停用的分组现在不服务请求，不能标「可用」')
        self.assertNotIn('http://g2.invalid', {c['base'] for c in fake.calls})


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
