"""图片/能力门禁与 search/mcp 配置段的校验与写入路径（迁移 Todo 11）。

## 为什么这些字段必须在这里拦

上游（core）对这几个键是 **fail-fast**：`upstream.image_policy` 写错、
`upstream.max_images` 为负、`search.enabled` 写成字符串 `"false"` —— 都会让
Go 侧 `json.Unmarshal` 或 `normalize()` 报错，**进程起不来**。

面板保存是直接 `POST /api/settings/upstream` 透传 body 的，不经过前端表单。
不在这里兜底，用户拿到的就是「保存成功 → 上游进入 Restarting 崩溃循环」，
这是本仓库最难查的一类故障（`_sanitize_section` 的注释里点名过多次）。

## 非默认分组为什么必须拒绝

`save_upstream_config` 写的是**默认 upstream 的** `config.json`。多分组下，
把 A 组的 `upstream.max_images` 写进默认文件，会让「A 组的上限」静默变成
「默认组的上限」—— 隔离失效，且面板上完全看不出来。所以非默认分组要么走
它自己的 panel API（`save_upstream_config_remote`），要么明确报错。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import wb2api  # noqa: E402


class _Resp:
    def __init__(self, status: int, text: str = '', payload: dict | None = None) -> None:
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError('no json body')
        return self._payload


class _Client:
    def __init__(self, resp: _Resp, calls: list) -> None:
        self._resp = resp
        self._calls = calls

    async def __aenter__(self) -> '_Client':
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, **kw: object) -> _Resp:
        self._calls.append((url, kw))
        return self._resp


class _CfgCase(unittest.TestCase):
    """每个用例一份独立的 config.json（与既有 test_config_io 同一套路）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg_path = Path(self._tmp.name) / 'config.json'
        self.cfg_path.write_text(json.dumps({'api_key': 'k'}), encoding='utf-8')
        patcher = mock.patch.object(config, 'UPSTREAM_CONFIG', self.cfg_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def on_disk(self) -> dict:
        return json.loads(self.cfg_path.read_text(encoding='utf-8'))


class EditableSectionsTest(unittest.TestCase):
    def test_search_and_mcp_are_editable(self) -> None:
        self.assertIn('search', wb2api._EDITABLE_SECTIONS)
        self.assertIn('mcp', wb2api._EDITABLE_SECTIONS)

    def test_profiles_is_editable_even_before_core_lands_it(self) -> None:
        """Todo 20 才在 core 落地 profiles.*；白名单先加，否则将来会有一段
        「界面能填、存不下去」的窗口期。"""
        self.assertIn('profiles', wb2api._EDITABLE_SECTIONS)


class SanitizeToolFlagsTest(unittest.TestCase):
    def test_accepts_valid_tool_flag_payload(self) -> None:
        out = wb2api._sanitize_section('search', {'enabled': True})
        self.assertIs(out['enabled'], True)
        out = wb2api._sanitize_section('mcp', {'enabled': False})
        self.assertIs(out['enabled'], False)
        out = wb2api._sanitize_section('profiles', {'enabled': True, 'detect_only': False})
        self.assertIs(out['enabled'], True)
        self.assertIs(out['detect_only'], False)
        out = wb2api._sanitize_section('upstream', {
            'max_images': 8, 'image_policy': ' truncate ', 'capability_guard': True})
        self.assertEqual(out['max_images'], 8)
        self.assertEqual(out['image_policy'], 'truncate', '策略应去空白并归一为小写')
        self.assertIs(out['capability_guard'], True)

    def test_max_images_zero_is_unlimited_and_valid(self) -> None:
        """0 = 不限，是合法哨兵值，不能被当成「未设置」丢掉。"""
        out = wb2api._sanitize_section('upstream', {'max_images': 0})
        self.assertEqual(out['max_images'], 0)

    def test_all_policies_accepted(self) -> None:
        for policy in ('passthrough', 'truncate', 'error'):
            with self.subTest(policy=policy):
                out = wb2api._sanitize_section('upstream', {'image_policy': policy})
                self.assertEqual(out['image_policy'], policy)

    def test_rejects_string_bool(self) -> None:
        """字符串 "false" 在 Go 侧是 JSON 类型错误 → 上游启动失败。"""
        for section, key in (('search', 'enabled'), ('mcp', 'enabled'),
                             ('profiles', 'enabled'), ('profiles', 'detect_only'),
                             ('upstream', 'capability_guard')):
            with self.subTest(section=section, key=key):
                with self.assertRaises(ValueError):
                    wb2api._sanitize_section(section, {key: 'false'})

    def test_rejects_non_int_max_images(self) -> None:
        for bad in ('8', 8.5, None, True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    wb2api._sanitize_section('upstream', {'max_images': bad})

    def test_rejects_negative_max_images(self) -> None:
        with self.assertRaises(ValueError):
            wb2api._sanitize_section('upstream', {'max_images': -1})

    def test_rejects_bad_policy(self) -> None:
        for bad in ('bogus', '', None, 1):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    wb2api._sanitize_section('upstream', {'image_policy': bad})


class SaveToolFlagsTest(_CfgCase):
    def test_save_writes_all_tool_flags(self) -> None:
        wb2api.save_upstream_config({
            'upstream': {'max_images': 6, 'image_policy': 'error', 'capability_guard': True},
            'search': {'enabled': True},
            'mcp': {'enabled': True},
        })
        cfg = self.on_disk()
        self.assertEqual(cfg['upstream']['max_images'], 6)
        self.assertEqual(cfg['upstream']['image_policy'], 'error')
        self.assertIs(cfg['upstream']['capability_guard'], True)
        self.assertIs(cfg['search']['enabled'], True)
        self.assertIs(cfg['mcp']['enabled'], True)

    def test_invalid_value_writes_nothing(self) -> None:
        before = self.on_disk()
        for patch in ({'search': {'enabled': 'true'}},
                      {'upstream': {'max_images': '4'}},
                      {'upstream': {'image_policy': 'bogus'}}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                wb2api.save_upstream_config(patch)
            self.assertEqual(self.on_disk(), before, '校验失败时不得改动磁盘配置')

    def test_default_upstream_id_is_accepted(self) -> None:
        for uid in (None, '', 0, '0'):
            with self.subTest(upstream_id=uid):
                wb2api.save_upstream_config({'search': {'enabled': True}}, upstream_id=uid)
                self.assertIs(self.on_disk()['search']['enabled'], True)

    def test_non_default_upstream_is_rejected(self) -> None:
        """非默认分组绝不能写默认文件（隔离失效且无任何报错）。"""
        before = self.on_disk()
        for uid in (3, '3'):
            with self.subTest(upstream_id=uid), self.assertRaises(ValueError) as ctx:
                wb2api.save_upstream_config({'upstream': {'max_images': 1}}, upstream_id=uid)
            self.assertIn('默认上游', str(ctx.exception))
            self.assertEqual(self.on_disk(), before)


class SaveRemoteTest(unittest.TestCase):
    """非默认分组：走它自己的 base_url + forward_api_key。"""

    def test_posts_to_own_base_with_forward_key(self) -> None:
        calls: list = []

        def fake_client(*a: object, **kw: object) -> _Client:
            return _Client(_Resp(200, payload={'ok': True, 'restart_required': []}), calls)

        upstream = {'id': 3, 'name': 'A 组', 'base_url': 'http://10.0.0.9:7863',
                    'api_key': 'group-key', 'enabled': True}
        with mock.patch.object(config, 'http_client', fake_client):
            out = asyncio.run(wb2api.save_upstream_config_remote(
                upstream, {'search': {'enabled': True}}))
        self.assertTrue(out['ok'])
        self.assertEqual(out['base_url'], 'http://10.0.0.9:7863')
        url, kw = calls[0]
        self.assertEqual(url, 'http://10.0.0.9:7863/panel/api/config',
                         '必须打到该分组自己的地址')
        self.assertNotIn('write=0', url, '本机没有它的 config.json，写盘权归它自己')
        self.assertEqual(kw['headers'].get('Authorization'), 'Bearer group-key')
        self.assertEqual(kw['json'], {'search': {'enabled': True}})

    def test_local_validation_prevents_bad_remote_write(self) -> None:
        calls: list = []

        def fake_client(*a: object, **kw: object) -> _Client:
            return _Client(_Resp(200, payload={'ok': True}), calls)

        upstream = {'id': 3, 'base_url': 'http://10.0.0.9:7863', 'enabled': True}
        with mock.patch.object(config, 'http_client', fake_client), \
                self.assertRaises(ValueError):
            asyncio.run(wb2api.save_upstream_config_remote(
                upstream, {'upstream': {'image_policy': 'bogus'}}))
        self.assertEqual(calls, [], '非法值不该打一趟上游')

    def test_rejected_by_remote_surfaces_as_value_error(self) -> None:
        calls: list = []

        def fake_client(*a: object, **kw: object) -> _Client:
            return _Client(_Resp(400, payload={'error': 'upstream.image_policy: bad'}), calls)

        upstream = {'id': 3, 'base_url': 'http://10.0.0.9:7863', 'enabled': True}
        with mock.patch.object(config, 'http_client', fake_client), \
                self.assertRaises(ValueError) as ctx:
            asyncio.run(wb2api.save_upstream_config_remote(
                upstream, {'upstream': {'image_policy': 'error'}}))
        self.assertIn('image_policy', str(ctx.exception))

    def test_unsupported_remote_is_explicit(self) -> None:
        calls: list = []

        def fake_client(*a: object, **kw: object) -> _Client:
            return _Client(_Resp(404, '404 page not found'), calls)

        upstream = {'id': 3, 'base_url': 'http://10.0.0.9:7863', 'enabled': True}
        with mock.patch.object(config, 'http_client', fake_client), \
                self.assertRaises(ValueError) as ctx:
            asyncio.run(wb2api.save_upstream_config_remote(
                upstream, {'search': {'enabled': True}}))
        self.assertIn('不支持', str(ctx.exception))

    def test_disabled_upstream_is_rejected(self) -> None:
        upstream = {'id': 3, 'base_url': 'http://10.0.0.9:7863', 'enabled': False}
        with self.assertRaises(ValueError):
            asyncio.run(wb2api.save_upstream_config_remote(
                upstream, {'search': {'enabled': True}}))


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
