"""预迁移 golden 的回归测试（W0）。

判据
----
迁移前的行为必须被**逐字节**钉住：跑真实代码路径重新采集一次，与提交进仓库的
golden 逐字比对。这样任何改动（无论有意无意）都会在这里现形，而不是等到
F4 的 scope 门禁或线上回归才发现。

为什么不是「读 golden 再断言几个字段」
--------------------------------------
字段级断言只能覆盖作者想到的字段。多出来的字段（例如面板侧新补的 `modalities`）
不会让字段级断言失败，却会改变客户端看到的东西 —— 而这次迁移的验收标准恰恰是
「flag 全关时响应与 golden 逐字一致」。所以比对的是**整个文件**。

采集器与 golden 的关系见 `server/tests/golden/README.md`。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.tests import golden_capture, settings_defaults  # noqa: E402

GOLDEN_DIR = golden_capture.GOLDEN_DIR


class GoldenFilesExistTest(unittest.TestCase):
    """五个 golden 文件必须都在（少一个就说明基线不完整）。"""

    def test_all_golden_files_present(self) -> None:
        expected = {
            'chat_request_body.json',
            'responses_request_body.json',
            'anthropic_request_body.json',
            'models_listing.json',
            'retention_defaults.json',
        }
        present = {p.name for p in GOLDEN_DIR.glob('*.json')}
        missing = expected - present
        self.assertFalse(missing, f'缺少 golden 文件：{sorted(missing)}')

    def test_golden_files_are_valid_json(self) -> None:
        for path in sorted(GOLDEN_DIR.glob('*.json')):
            with self.subTest(path=path.name):
                json.loads(path.read_text(encoding='utf-8'))


class GoldenReproductionTest(unittest.TestCase):
    """重新采集一次，与提交的 golden 逐字节比对。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.captured = golden_capture.build_payloads()

    def _assert_matches(self, name: str) -> None:
        path = GOLDEN_DIR / name
        committed = path.read_text(encoding='utf-8')
        self.assertEqual(
            self.captured[name], committed,
            f'{name} 与提交的 golden 不一致 —— 若本次改动是有意的，'
            f'跑 `python3 -m server.tests.golden_capture` 重新生成并在提交说明里写清差异')

    def test_chat_request_body_matches(self) -> None:
        self._assert_matches('chat_request_body.json')

    def test_responses_request_body_matches(self) -> None:
        self._assert_matches('responses_request_body.json')

    def test_anthropic_request_body_matches(self) -> None:
        self._assert_matches('anthropic_request_body.json')

    def test_models_listing_matches(self) -> None:
        self._assert_matches('models_listing.json')

    def test_retention_defaults_matches(self) -> None:
        self._assert_matches('retention_defaults.json')

    def test_capture_is_deterministic(self) -> None:
        """同一进程内再采集一次，结果必须完全相同（确定性自证）。"""
        again = golden_capture.build_payloads()
        self.assertEqual(self.captured, again, '两次采集结果不同 —— golden 不可复现')


class GoldenShapeTest(unittest.TestCase):
    """golden 的**内容**也要对：三个协议确实各是一条 chat 请求体。

    纯比对有个盲点：如果采集器本身退化成「三个协议都录了同一份」，比对依然全绿。
    这里断言各协议的**区别**，把「录错协议」这一类错误挡住。
    """

    def test_three_protocols_produce_chat_shaped_bodies(self) -> None:
        for name in ('chat_request_body.json', 'responses_request_body.json',
                     'anthropic_request_body.json'):
            with self.subTest(name=name):
                body = json.loads((GOLDEN_DIR / name).read_text(encoding='utf-8'))
                self.assertIn('messages', body, f'{name} 缺 messages（上游只认 chat 协议）')
                self.assertEqual(body['model'], 'glm-5.2')
                self.assertEqual(body['max_tokens'], 256)
                self.assertEqual(body['messages'][0]['role'], 'system')

    def test_chat_keeps_temperature_others_do_not(self) -> None:
        """chat 透传 temperature；Responses / Anthropic 的翻译层不搬这个字段。"""
        chat = json.loads((GOLDEN_DIR / 'chat_request_body.json').read_text(encoding='utf-8'))
        self.assertEqual(chat['temperature'], 0.2)
        for name in ('responses_request_body.json', 'anthropic_request_body.json'):
            with self.subTest(name=name):
                body = json.loads((GOLDEN_DIR / name).read_text(encoding='utf-8'))
                self.assertNotIn('temperature', body)

    def test_models_listing_has_compat_fields(self) -> None:
        """面板侧补齐的兼容字段必须在 golden 里（这是它存在的意义）。"""
        listing = json.loads((GOLDEN_DIR / 'models_listing.json').read_text(encoding='utf-8'))
        ids = [item['id'] for item in listing['data']]
        self.assertEqual(ids, ['glm-5.2', 'deepseek-v4.1-flash'])
        for item in listing['data']:
            with self.subTest(model=item['id']):
                self.assertIn('modalities', item)
                self.assertIn('supported_reasoning_levels', item)

    def test_retention_defaults_match_runtime_constants(self) -> None:
        """保留期 golden 的值必须等于运行时真正用的常量（不是抄来的字面量）。"""
        from server import db as dbmod

        golden = json.loads((GOLDEN_DIR / 'retention_defaults.json').read_text(encoding='utf-8'))
        self.assertEqual(golden['_REQUEST_LOG_RETAIN_DAYS'], dbmod._REQUEST_LOG_RETAIN_DAYS)
        self.assertEqual(golden['_HOURLY_RETAIN_DAYS'], dbmod._HOURLY_RETAIN_DAYS)
        self.assertEqual(golden['_TASK_LOG_RETAIN_DAYS'], dbmod._TASK_LOG_RETAIN_DAYS)
        self.assertEqual(golden['_AUDIT_LOG_MAX_ROWS'], dbmod._AUDIT_LOG_MAX_ROWS)
        self.assertEqual(golden['_REQUEST_LOG_CHECK_EVERY'], dbmod._REQUEST_LOG_CHECK_EVERY)
        self.assertEqual(golden['log_retention_overrides'], {})


class SettingsDefaultsTest(unittest.TestCase):
    """19 个 flag 的集中定义（F4 依赖它解析默认值）。"""

    def test_exactly_nineteen_flags(self) -> None:
        self.assertEqual(len(settings_defaults.FLAG_DEFAULTS), settings_defaults.FLAG_COUNT)

    def test_flag_names_match_contract(self) -> None:
        expected = {
            'model_routes_enabled', 'log_retention_overrides',
            'responses_projection_mode', 'responses_projection_max_item_bytes',
            'tool_repair', 'tool_repair_recall',
            'billing_credit_price_usd', 'billing_usd_rate', 'billing_currency',
            'catalog_group_merge', 'tools_search_enabled', 'tools_mcp_enabled',
            'upstream.max_images', 'upstream.image_policy', 'upstream.capability_guard',
            'profiles.enabled', 'profiles.detect_only',
            'search.enabled', 'mcp.enabled',
        }
        self.assertEqual(set(settings_defaults.FLAG_DEFAULTS), expected)

    def test_defaults_match_contract_values(self) -> None:
        d = settings_defaults.FLAG_DEFAULTS
        self.assertIs(d['model_routes_enabled'], False)
        self.assertEqual(d['log_retention_overrides'], {})
        self.assertEqual(d['responses_projection_mode'], 'passthrough')
        self.assertEqual(d['responses_projection_max_item_bytes'], 40000)
        self.assertIs(d['tool_repair'], False)
        self.assertIs(d['tool_repair_recall'], False)
        self.assertEqual(d['billing_credit_price_usd'], 0.03)
        self.assertEqual(d['billing_usd_rate'], 7.15)
        self.assertEqual(d['billing_currency'], 'usd')
        self.assertIs(d['catalog_group_merge'], False)
        self.assertIs(d['tools_search_enabled'], False)
        self.assertIs(d['tools_mcp_enabled'], False)
        self.assertEqual(d['upstream.max_images'], 0)
        self.assertEqual(d['upstream.image_policy'], 'passthrough')
        self.assertIs(d['upstream.capability_guard'], False)
        self.assertIs(d['profiles.enabled'], False)
        self.assertIs(d['profiles.detect_only'], True)
        self.assertIs(d['search.enabled'], False)
        self.assertIs(d['mcp.enabled'], False)

    def test_owner_partition(self) -> None:
        """12 个 manager flag + 7 个 core flag = 19，且没有交叉。"""
        manager = settings_defaults.flags_owned_by('manager')
        core = settings_defaults.flags_owned_by('core')
        self.assertEqual(len(manager), 12)
        self.assertEqual(len(core), 7)
        self.assertFalse(set(manager) & set(core))
        self.assertEqual(set(manager) | set(core), set(settings_defaults.FLAG_DEFAULTS))

    def test_text_round_trip(self) -> None:
        """导出的文本能被 parse_defaults 原样读回（F4 用的就是这条路径）。"""
        text = settings_defaults.flag_defaults_text()
        parsed = settings_defaults.parse_defaults(text)
        self.assertEqual(parsed, settings_defaults.FLAG_DEFAULTS)
        self.assertEqual(settings_defaults.assert_contract(parsed), [])

    def test_table_parser_reads_contract_table(self) -> None:
        """契约文档里那种 markdown 表格也能解析（同一份事实的另一种载体）。

        同时钉住「别的 5 列表格不会被误当 flag」：契约文档里还有 profile 的 base
        URL 表（第 1 列同样带反引号），只按列形状匹配会把它也读进来。
        """
        table = '\n'.join([
            '| flag | repo | storage | default | off behavior |',
            '|---|---|---|---|---|',
            '| `model_routes_enabled` | manager | `settings` | `false` | 走旧 `model_map` |',
            '| `billing_currency` | manager | `settings` | `usd` | 不输出 cny 字段 |',
            '| `cn-work` | https://www.codebuddy.cn | cn | realm=cn | 旧路由 |',
        ])
        parsed = settings_defaults.parse_table(table)
        self.assertEqual(parsed, {'model_routes_enabled': False, 'billing_currency': 'usd'})

    def test_assert_contract_reports_mismatch(self) -> None:
        """差异检测本身要有效（否则门禁是装饰品）。"""
        broken = dict(settings_defaults.FLAG_DEFAULTS)
        broken['tool_repair'] = True
        del broken['mcp.enabled']
        broken['unknown_flag'] = 1
        problems = settings_defaults.assert_contract(broken)
        self.assertTrue(any('tool_repair' in p for p in problems))
        self.assertTrue(any('mcp.enabled' in p for p in problems))
        self.assertTrue(any('unknown_flag' in p for p in problems))


if __name__ == '__main__':
    unittest.main()
