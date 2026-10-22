"""工具参数修复（`tool_repair` / `tool_repair_recall`，契约 §5 / Todo 15）。

本功能是**新功能**，明确偏离参考实现（codebuddy2api 是整请求重试，不是 JSON
修复）。判据：

  · 只有 `tool_repair=true` 才动参数；关闭时非法 JSON 原样透传、日志记 `none`；
  · 确定性修复：去 markdown fence、去尾逗号、补齐括号、提取首个 JSON 对象；
  · **绝不发明值**：修不好就原样返回，不塞 `{}`、不猜字段；
  · `recall` 只在非流式（聚合）模式、`tool_repair_recall=true` 时最多 1 次，
    发送同一 body + system 指令；流式模式不 recall；
  · 修不好**不返回 502**（与参考实现的关键差异），原样返回并记 `failed`；
  · 额外那次 recall 请求同样写一行 `request_logs`；
  · `request_logs.tool_repair` 只接受 `none|fixed|failed|recalled`。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc  # noqa: E402
from server.routers import responses as R  # noqa: E402


# ── 纯函数：确定性修复 ───────────────────────────────────────

class RepairFunctionTest(unittest.TestCase):
    def test_valid_object_is_untouched(self) -> None:
        self.assertEqual(R.repair_tool_arguments('{"a":1}'), ('{"a":1}', 'none'))

    def test_trailing_comma(self) -> None:
        self.assertEqual(R.repair_tool_arguments('{"a":1,}'), ('{"a":1}', 'fixed'))

    def test_nested_trailing_commas(self) -> None:
        self.assertEqual(R.repair_tool_arguments('{"a":[1,2,],"b":2,}'),
                         ('{"a":[1,2],"b":2}', 'fixed'))

    def test_markdown_fence(self) -> None:
        raw = '```json\n{"a": 1}\n```'
        self.assertEqual(R.repair_tool_arguments(raw), ('{"a": 1}', 'fixed'))

    def test_fence_with_prose(self) -> None:
        raw = 'Here you go:\n```\n{"a": 1}\n```\nthanks'
        self.assertEqual(R.repair_tool_arguments(raw)[0], '{"a": 1}')

    def test_missing_closing_brace(self) -> None:
        self.assertEqual(R.repair_tool_arguments('{"a":1'), ('{"a":1}', 'fixed'))

    def test_missing_nested_brackets(self) -> None:
        self.assertEqual(R.repair_tool_arguments('{"a":[1,2'), ('{"a":[1,2]}', 'fixed'))

    def test_first_complete_object_wins(self) -> None:
        raw = 'noise {"a":1} tail {"b":2}'
        self.assertEqual(R.repair_tool_arguments(raw), ('{"a":1}', 'fixed'))

    def test_commas_inside_strings_are_preserved(self) -> None:
        raw = '{"a":",}",}'
        self.assertEqual(R.repair_tool_arguments(raw), ('{"a":",}"}', 'fixed'))

    def test_never_invents_missing_values(self) -> None:
        for raw in ('{"a":', '{"a":1 "b":2}', 'not json at all', '{"a":undefined}'):
            with self.subTest(raw=raw):
                self.assertEqual(R.repair_tool_arguments(raw), (raw, 'failed'))

    def test_scalar_is_not_a_tool_argument_object(self) -> None:
        self.assertEqual(R.repair_tool_arguments('123'), ('123', 'failed'))

    def test_non_string_is_not_repaired(self) -> None:
        self.assertEqual(R.repair_tool_arguments(None), (None, 'none'))


# ── HTTP 端点：真实转换链路，只把上游 HTTP 换成本地假对象 ─────────

class _Resp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> dict:
        return self._payload

    async def aclose(self) -> None:
        return None


class _Client:
    """按调用顺序返回预设响应，并记下每次发往上游的 body。"""

    def __init__(self, responses: list) -> None:
        self.requests: list[dict] = []
        self._responses = list(responses)

    async def post(self, url, *, json, **kw):
        self.requests.append(json)
        return self._responses.pop(0)

    def build_request(self, method, url, **kwargs):
        self.requests.append(kwargs['json'])
        return object()

    async def send(self, request, stream=True):
        return self._responses.pop(0)

    async def aclose(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _chat_payload(args: str, name: str = 'f') -> dict:
    return {
        'choices': [{
            'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_1', 'type': 'function',
                 'function': {'name': name, 'arguments': args}},
            ]},
            'finish_reason': 'tool_calls',
        }],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 1},
    }


class _EndpointBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)
        self._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = self._dir / 'repair.db'
        config.USERS_FILE = self._dir / 'users.json'
        config.STATIC_DIR = self._dir / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        self.client = TestClient(app)
        self.key = keysvc.create_key(name='repair-test')['key']

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _run(self, client: _Client, post) -> object:
        upstream = {'base_url': 'http://mock-upstream.invalid', 'api_key': 'k',
                    'is_default': False}
        with mock.patch.object(config, 'http_client', lambda *a, **k: client), \
             mock.patch.object(R.upstreamsvc, 'resolve_for_key', return_value=upstream):
            return post()

    def _last_log(self) -> dict:
        return db.query_one('SELECT * FROM request_logs ORDER BY id DESC LIMIT 1')

    def _log_count(self) -> int:
        return db.query_one('SELECT COUNT(*) AS c FROM request_logs')['c']


class ResponsesRepairTest(_EndpointBase):
    def _post(self, args: str, *, stream: bool = False):
        body = {'model': 'glm-5.2', 'input': 'go',
                'tools': [{'type': 'function', 'name': 'f'}], 'stream': stream}
        return self.client.post('/v1/responses', json=body,
                                headers={'Authorization': f'Bearer {self.key}'})

    def _post_args(self, args: str, responses: list, *, stream: bool = False):
        client = _Client(responses)
        r = self._run(client, lambda: self._post(args, stream=stream))
        return r, client

    def test_flag_off_keeps_invalid_arguments_byte_for_byte(self) -> None:
        """flag 关闭 = golden：非法 JSON 原样透传，日志记 none。"""
        r, _client = self._post_args('{"a":1,}', [_Resp(200, _chat_payload('{"a":1,}'))])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['output'][0]['arguments'], '{"a":1,}')
        self.assertEqual(self._last_log()['tool_repair'], 'none')

    def test_trailing_comma_is_repaired(self) -> None:
        db.set_setting('tool_repair', True)
        r, _client = self._post_args('{"a":1,}', [_Resp(200, _chat_payload('{"a":1,}'))])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(json.loads(r.json()['output'][0]['arguments']), {'a': 1})
        self.assertEqual(self._last_log()['tool_repair'], 'fixed')

    def test_markdown_fence_is_repaired(self) -> None:
        db.set_setting('tool_repair', True)
        raw = '```json\n{"a": 1}\n```'
        r, _client = self._post_args(raw, [_Resp(200, _chat_payload(raw))])
        self.assertEqual(json.loads(r.json()['output'][0]['arguments']), {'a': 1})
        self.assertEqual(self._last_log()['tool_repair'], 'fixed')

    def test_missing_closing_brace_is_repaired(self) -> None:
        db.set_setting('tool_repair', True)
        r, _client = self._post_args('{"a":1', [_Resp(200, _chat_payload('{"a":1'))])
        self.assertEqual(json.loads(r.json()['output'][0]['arguments']), {'a': 1})
        self.assertEqual(self._last_log()['tool_repair'], 'fixed')

    def test_unrecoverable_returns_original_with_200(self) -> None:
        """修不好：原样返回 + failed + **不 502**（明确偏离参考实现）。"""
        db.set_setting('tool_repair', True)
        r, _client = self._post_args('{"a":', [_Resp(200, _chat_payload('{"a":'))])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['output'][0]['arguments'], '{"a":')
        self.assertEqual(self._last_log()['tool_repair'], 'failed')

    def test_recall_sends_instruction_and_is_logged_as_extra_request(self) -> None:
        """聚合模式 + recall 开：恰好 1 次额外上游调用，并写第二行日志。"""
        db.set_setting('tool_repair', True)
        db.set_setting('tool_repair_recall', True)
        r, client = self._post_args('{"a":', [
            _Resp(200, _chat_payload('{"a":')),
            _Resp(200, _chat_payload('{"a":1}')),
        ])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(client.requests), 2, '应恰好 recall 一次')
        self.assertEqual(json.loads(r.json()['output'][0]['arguments']), {'a': 1})
        self.assertIn(R.TOOL_REPAIR_RECALL_INSTRUCTION,
                      json.dumps(client.requests[1], ensure_ascii=False))
        self.assertEqual(self._log_count(), 2, '额外请求必须计入 request_logs')
        self.assertEqual(self._last_log()['tool_repair'], 'recalled')

    def test_recall_happens_at_most_once(self) -> None:
        """recall 之后仍不可修复：不再重试，返回原样。"""
        db.set_setting('tool_repair', True)
        db.set_setting('tool_repair_recall', True)
        r, client = self._post_args('{"a":', [
            _Resp(200, _chat_payload('{"a":')),
            _Resp(200, _chat_payload('{"a":')),
        ])
        self.assertEqual(len(client.requests), 2, '最多一次 recall')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['output'][0]['arguments'], '{"a":')
        self.assertEqual(self._last_log()['tool_repair'], 'failed')

    def test_recall_disabled_keeps_single_upstream_call(self) -> None:
        db.set_setting('tool_repair', True)
        r, client = self._post_args('{"a":', [_Resp(200, _chat_payload('{"a":'))])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(r.status_code, 200)

    def test_streaming_never_recalls(self) -> None:
        db.set_setting('tool_repair', True)
        db.set_setting('tool_repair_recall', True)
        frames = b''.join([
            b'data: ' + json.dumps({'choices': [{'delta': {'tool_calls': [
                {'index': 0, 'id': 'call_1',
                 'function': {'name': 'f', 'arguments': '{"a":'}}]}}]}).encode() + b'\n\n',
            b'data: ' + json.dumps({'choices': [{'delta': {},
                'finish_reason': 'tool_calls'}]}).encode() + b'\n\n',
            b'data: [DONE]\n\n',
        ])

        class _StreamResp:
            status_code = 200

            async def aiter_bytes(self):
                yield frames

            async def aclose(self) -> None:
                return None

        client = _Client([_StreamResp()])
        r = self._run(client, lambda: self._post('{"a":', stream=True))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(client.requests), 1, '流式模式不得 recall')
        self.assertEqual(self._last_log()['tool_repair'], 'none')


class AnthropicRepairTest(_EndpointBase):
    def _post(self, args: str, stream: bool = False):
        body = {'model': 'glm-5.2', 'max_tokens': 32, 'stream': stream,
                'messages': [{'role': 'user', 'content': 'go'}],
                'tools': [{'name': 'f', 'input_schema': {'type': 'object'}}]}
        return self.client.post('/v1/messages', json=body,
                                headers={'x-api-key': self.key})

    def _post_args(self, args: str, responses: list, *, stream: bool = False):
        client = _Client(responses)
        r = self._run(client, lambda: self._post(args, stream=stream))
        return r, client

    def test_trailing_comma_is_repaired(self) -> None:
        db.set_setting('tool_repair', True)
        r, _client = self._post_args('{"a":1,}', [_Resp(200, _chat_payload('{"a":1,'))])
        self.assertEqual(r.status_code, 200, r.text)
        block = r.json()['content'][0]
        self.assertEqual(block['type'], 'tool_use')
        self.assertEqual(block['input'], {'a': 1})
        self.assertEqual(self._last_log()['tool_repair'], 'fixed')

    def test_flag_off_keeps_raw_argument(self) -> None:
        r, _client = self._post_args('{"a":1,}', [_Resp(200, _chat_payload('{"a":1,'))])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['content'][0]['input'], {'_raw': '{"a":1,'})
        self.assertEqual(self._last_log()['tool_repair'], 'none')

    def test_unrecoverable_keeps_raw_and_does_not_502(self) -> None:
        db.set_setting('tool_repair', True)
        r, _client = self._post_args('{"a":', [_Resp(200, _chat_payload('{"a":'))])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['content'][0]['input'], {'_raw': '{"a":'})
        self.assertEqual(self._last_log()['tool_repair'], 'failed')

    def test_recall_sends_instruction_once(self) -> None:
        db.set_setting('tool_repair', True)
        db.set_setting('tool_repair_recall', True)
        r, client = self._post_args('{"a":', [
            _Resp(200, _chat_payload('{"a":')),
            _Resp(200, _chat_payload('{"a":1}')),
        ])
        self.assertEqual(len(client.requests), 2)
        self.assertIn(R.TOOL_REPAIR_RECALL_INSTRUCTION,
                      json.dumps(client.requests[1], ensure_ascii=False))
        self.assertEqual(r.json()['content'][0]['input'], {'a': 1})
        self.assertEqual(self._log_count(), 2)
        self.assertEqual(self._last_log()['tool_repair'], 'recalled')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
