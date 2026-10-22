"""Responses 逐项上下文投影（`project_items`，契约 §5 / Todo 14）。

判据全部围绕「**逐 item** 的字节预算」这条契约：

  · 预算 = content 文本 + tool_result 文本；tool_call 的 `arguments` 永不截断
    （截断它只会得到非法 JSON，上游直接 400）；
  · 超限按 60% 头 + `\\n...[truncated N bytes]...\\n` + 40% 尾；
  · 不删除整个 item、不拆 tool 对；
  · instructions / system 永远完整；
  · 单 item 无法压缩时回退 passthrough 并记日志；
  · flag 关闭（默认）时输出与提交的 golden 逐字节一致。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import responses as R  # noqa: E402

MARKER_RE = re.compile(r'\n\.\.\.\[truncated (\d+) bytes\]\.\.\.\n')

MODE_KEY = 'responses_projection_mode'
MAX_KEY = 'responses_projection_max_item_bytes'


def _text_of(item: dict) -> str:
    """把 message/tool_output item 的文本 part 拼起来（与投影口径一致）。"""
    kind = item.get('type')
    value = item.get('output') if kind in (
        'function_call_output', 'custom_tool_call_output') else item.get('content')
    if isinstance(value, str):
        return value
    return ''.join(p.get('text') or '' for p in value or [] if isinstance(p, dict))


def _bytes(text: str) -> int:
    return len(text.encode('utf-8'))


class ProjectionTestBase(unittest.TestCase):
    """投影直接读 `settings` 表，所以每个用例都跑在临时 DB 上。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'projection.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _enable(self, mode: str = 'balanced', budget: int = 40000) -> None:
        db.set_setting(MODE_KEY, mode)
        db.set_setting(MAX_KEY, budget)


class FlagOffTest(ProjectionTestBase):
    """默认 passthrough：不投影，逐字节不变。"""

    def test_default_settings_leave_body_untouched(self) -> None:
        body = {'model': 'm', 'input': [
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'A' * 120000}]}]}
        self.assertEqual(R.project_items(body), body)

    def test_explicit_passthrough_ignores_budget(self) -> None:
        self._enable('passthrough', 40000)
        body = {'model': 'm', 'input': 'A' * 120000}
        self.assertEqual(R.project_items(body), body)

    def test_flag_off_output_matches_committed_golden(self) -> None:
        """真实路由（flag 默认关）发出的请求体必须与 golden 逐字一致。"""
        from server.tests import golden_capture

        captured = golden_capture.build_payloads()['responses_request_body.json']
        committed = (golden_capture.GOLDEN_DIR / 'responses_request_body.json').read_text(
            encoding='utf-8')
        self.assertEqual(captured, committed)


class SinglePartTest(ProjectionTestBase):
    def test_long_single_part_split_head_tail_with_marker(self) -> None:
        self._enable(budget=40000)
        text = 'A' * 40000 + 'B' * 40000 + 'C' * 40000
        body = {'model': 'm', 'input': [
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': text}]}]}

        out = R.project_items(body)
        projected = out['input'][0]['content'][0]['text']

        self.assertLessEqual(_bytes(projected), 40000, '投影后超过 item 预算')
        match = MARKER_RE.search(projected)
        self.assertIsNotNone(match, f'缺少截断标记：{projected[:80]!r}')
        self.assertTrue(projected.startswith('A'), '头部内容被丢掉了')
        self.assertTrue(projected.endswith('C'), '尾部内容被丢掉了')
        self.assertNotIn('B', projected, '中间段应当被压掉')
        # 标记里的 N 必须等于真实移除的字节数。
        removed = 120000 - (_bytes(projected) - _bytes(match.group(0)))
        self.assertEqual(int(match.group(1)), removed)

    def test_zero_budget_disables_projection(self) -> None:
        self._enable(budget=0)
        text = 'A' * 120000
        body = {'model': 'm', 'input': [
            {'type': 'message', 'role': 'user', 'content': text}]}
        self.assertEqual(R.project_items(body), body)

    def test_system_message_stays_complete(self) -> None:
        self._enable(budget=40000)
        text = 'S' * 120000
        body = {'model': 'm', 'input': [
            {'type': 'message', 'role': 'system', 'content': text}]}
        self.assertEqual(R.project_items(body)['input'][0]['content'], text)

    def test_instructions_stay_complete(self) -> None:
        self._enable(budget=40000)
        instructions = 'I' * 120000
        body = {'model': 'm', 'instructions': instructions,
                'input': [{'type': 'message', 'role': 'user', 'content': 'A' * 120000}]}
        self.assertEqual(R.project_items(body)['instructions'], instructions)


class MultiPartTest(ProjectionTestBase):
    def test_parts_share_one_item_budget(self) -> None:
        self._enable(budget=40000)
        body = {'model': 'm', 'input': [
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'A' * 40000},
                {'type': 'input_text', 'text': 'B' * 40000},
                {'type': 'input_text', 'text': 'C' * 40000},
            ]}]}

        out = R.project_items(body)
        content = out['input'][0]['content']
        joined = ''.join(p.get('text') or '' for p in content)

        self.assertLessEqual(_bytes(joined), 40000, '多 part 合计仍超预算')
        self.assertEqual(len(MARKER_RE.findall(joined)), 1, '应当只有一个截断标记')
        self.assertTrue(joined.startswith('A'))
        self.assertTrue(joined.endswith('C'))

    def test_image_part_is_preserved(self) -> None:
        self._enable(budget=40000)
        image = {'type': 'input_image', 'image_url': 'data:image/png;base64,QUJD'}
        body = {'model': 'm', 'input': [
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'T' * 120000},
                image,
            ]}]}

        out = R.project_items(body)
        content = out['input'][0]['content']

        self.assertEqual(len(content), 2, 'item/part 数量被改变了')
        self.assertEqual(content[1], image, '图片块被改写或丢弃了')
        self.assertLessEqual(_bytes(content[0]['text']), 40000)
        self.assertRegex(content[0]['text'], MARKER_RE)


class ToolTest(ProjectionTestBase):
    def test_each_tool_output_gets_its_own_budget(self) -> None:
        """逐 item 预算：两个 100KB 结果各自压到 40KB，而不是共用一个总额。"""
        self._enable(budget=40000)
        body = {'model': 'm', 'input': [
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'X' * 100000},
            {'type': 'function_call_output', 'call_id': 'c2', 'output': 'Y' * 100000},
        ]}

        out = R.project_items(body)
        first, second = out['input']
        self.assertLessEqual(_bytes(first['output']), 40000)
        self.assertLessEqual(_bytes(second['output']), 40000)
        self.assertRegex(first['output'], MARKER_RE)
        self.assertRegex(second['output'], MARKER_RE)
        self.assertTrue(first['output'].startswith('X'))
        self.assertTrue(second['output'].startswith('Y'))

    def test_tool_call_arguments_are_never_truncated(self) -> None:
        self._enable(budget=40000)
        arguments = json.dumps({'payload': 'z' * 100000})
        body = {'model': 'm', 'input': [
            {'type': 'function_call', 'call_id': 'c1', 'name': 'write',
             'arguments': arguments},
        ]}

        with self.assertLogs('workbuddy.responses', level='WARNING') as logs:
            out = R.project_items(body)

        item = out['input'][0]
        self.assertEqual(item['arguments'], arguments, 'tool_call arguments 被截断了')
        self.assertEqual(json.loads(item['arguments'])['payload'][:3], 'zzz')
        self.assertTrue(any('passthrough' in line for line in logs.output),
                        f'回退 passthrough 没有记日志：{logs.output}')

    def test_tool_pair_is_never_deleted(self) -> None:
        self._enable(budget=40000)
        body = {'model': 'm', 'input': [
            {'type': 'function_call', 'call_id': 'c1', 'name': 'read',
             'arguments': '{"path":"a.txt"}'},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'R' * 100000},
        ]}

        out = R.project_items(body)
        kinds = [item['type'] for item in out['input']]
        self.assertEqual(kinds, ['function_call', 'function_call_output'], 'tool 对被拆开/删除')
        self.assertEqual(out['input'][0]['arguments'], '{"path":"a.txt"}')
        self.assertLessEqual(_bytes(out['input'][1]['output']), 40000)

        chat = R.to_chat_request(out)
        roles = [m['role'] for m in chat['messages']]
        self.assertEqual(roles, ['assistant', 'tool'], '翻译后 tool 对也不完整')
        self.assertEqual(json.loads(chat['messages'][0]['tool_calls'][0]['function']['arguments']),
                         {'path': 'a.txt'})


class SettingsValidationTest(ProjectionTestBase):
    def test_invalid_budget_falls_back_to_default(self) -> None:
        """合法值只有 0 或 >=256；非法值不能悄悄变成「关闭」或极小预算。"""
        self._enable(budget=100)
        text = 'A' * 50000
        body = {'model': 'm', 'input': [
            {'type': 'message', 'role': 'user', 'content': text}]}

        with self.assertLogs('workbuddy.responses', level='WARNING') as logs:
            out = R.project_items(body)

        projected = out['input'][0]['content']
        self.assertRegex(projected, MARKER_RE, '非法预算没有回退到默认 40000')
        self.assertLessEqual(_bytes(projected), 40000)
        self.assertTrue(any('responses_projection_max_item_bytes' in line
                            for line in logs.output), logs.output)

class EndpointProjectionTest(ProjectionTestBase):
    """真实路由：投影在发往上游的请求体里生效（不是只测纯函数）。"""

    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient
        from server import keysvc, security
        from server.main import app

        security.save_users({'secret': 'S', 'users': [], 'api_keys': []})
        self.token = keysvc.create_key('projection')['key']
        self.client = TestClient(app)

    def _post(self, payload: dict) -> dict:
        sent: list[dict] = []

        class _Resp:
            status_code = 200
            text = ''

            def json(self):
                return {'id': 'x', 'choices': [{'index': 0, 'message': {
                    'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
                    'usage': {}}

        class _Client:
            async def post(self, url, json=None, headers=None):
                sent.append(json)
                return _Resp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with mock.patch.object(config, 'http_client', lambda *a, **k: _Client()):
            r = self.client.post('/v1/responses',
                                 headers={'Authorization': f'Bearer {self.token}'},
                                 json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(sent), 1, '上游没有被调用')
        return sent[0]

    def test_100kb_part_is_projected_on_the_wire(self) -> None:
        self._enable(budget=40000)
        text = 'H' * 50000 + 'M' * 50000 + 'T' * 50000
        outbound = self._post({'model': 'glm-5.2', 'input': [
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': text}]}]})

        content = outbound['messages'][0]['content']
        self.assertLessEqual(_bytes(content), 40000)
        self.assertIsNotNone(MARKER_RE.search(content),
                             f'出站请求没有截断标记：{content[:80]!r}')
        self.assertTrue(content.startswith('H'), '头部没保留')
        self.assertTrue(content.endswith('T'), '尾部没保留')

    def test_adjacent_tool_pair_survives_on_the_wire(self) -> None:
        self._enable(budget=40000)
        outbound = self._post({'model': 'glm-5.2', 'input': [
            {'type': 'function_call', 'call_id': 'c1', 'name': 'read',
             'arguments': json.dumps({'path': 'a.txt'})},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'R' * 100000},
        ]})

        roles = [m['role'] for m in outbound['messages']]
        self.assertEqual(roles, ['assistant', 'tool'], 'tool 对在出站请求里不完整')
        call = outbound['messages'][0]['tool_calls'][0]
        self.assertEqual(json.loads(call['function']['arguments']), {'path': 'a.txt'})
        tool_text = outbound['messages'][1]['content']
        self.assertRegex(tool_text, MARKER_RE)
        self.assertLessEqual(_bytes(tool_text), 40000)

if __name__ == '__main__':
    unittest.main()
