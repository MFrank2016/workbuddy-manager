"""预迁移 golden 的**采集器**（测试辅助模块，不改变运行时行为）。

为什么要有采集器而不是手写 golden
--------------------------------
手写的「期望请求体」是**猜的**：它记录的是作者以为代码会发什么，而不是代码
实际发了什么。迁移前后要拿 golden 逐字比对，猜出来的基线会把两边的偏差一起
固化下来。所以这里的 golden 一律由**跑真实代码路径**产生：

  · 起真实的 FastAPI app（`server.main.app`），用 `TestClient` 走真实路由；
  · 把 `config.http_client` 换成 `httpx.MockTransport`——这是测试专用接缝，
    没有它就必须打真上游；换掉之后**发往上游的字节仍由真实转发代码生成**
    （鉴权、模型映射、max_tokens 钳制、协议翻译全都在），只是落到内存里；
  · 记录每个协议**实际发往上游**的请求体。

确定性
------
golden 必须逐字节可复现，否则「跑两次 diff」的证明毫无意义。这里的做法：

  · 时间、随机数、密钥一律不参与出站请求体（manager 的请求体里没有它们）；
  · JSON 统一 `sort_keys=True` + `ensure_ascii=False` + 两空格缩进 + 末尾换行；
  · 保留期默认值直接读 `server.db` 的模块常量（就是运行时真正用的那几个）；
  · 假上游的响应体是**写死的 fixture**（固定 id/时间戳），不取当前时间。

上游 fixture 里的 id 是固定的（`chatcmpl-golden-1`），因为 golden 只记录**请求**
与 `/v1/models` 清单，不记录响应体——响应体里的 `resp_<uuid>` / `msg_<uuid>`
天生不确定，录进来就等于每次都要归一化，反而弱化了「逐字一致」这个判据。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
from unittest import mock

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402

from server import config, db, keysvc  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / 'golden'

# 假上游凭据（合成值，不是真 token；提交进仓库也不含任何真实凭据）。
FIXTURE_UPSTREAM_KEY = 'golden-upstream-key'
FIXTURE_CLIENT_KEY_NAME = 'golden-client'

# 假上游的模型清单：字段集对齐上游 /v1/models 的形态（id/object/created/owned_by
# + 能力字段），固定值、固定顺序——manager 侧会再补 modalities 等兼容字段，
# 那部分正是 golden 要钉住的行为。
UPSTREAM_MODELS_PAYLOAD: dict[str, Any] = {
    'object': 'list',
    'data': [
        {
            'id': 'glm-5.2',
            'object': 'model',
            'created': 1753600000,
            'owned_by': 'workbuddy',
            'name': 'GLM-5.2',
            'description': '通用对话模型',
            'credits': 'x0.05',
            'supports_images': True,
            'supports_reasoning': True,
            'supports_tool_call': True,
            'reasoning_supported_efforts': ['low', 'high'],
            'reasoning_default_effort': 'high',
        },
        {
            'id': 'deepseek-v4.1-flash',
            'object': 'model',
            'created': 1753600000,
            'owned_by': 'workbuddy',
            'name': 'DeepSeek V4.1 Flash',
            'credits': 'x0.02',
            'supports_tool_call': True,
            'supports_reasoning': True,
            'reasoning_supported_efforts': ['high'],
            'reasoning_default_effort': 'high',
        },
    ],
}

# 假上游的 chat 响应：固定 id / 固定时间戳 / 固定 usage。
UPSTREAM_CHAT_PAYLOAD: dict[str, Any] = {
    'id': 'chatcmpl-golden-1',
    'object': 'chat.completion',
    'created': 1753600000,
    'model': 'glm-5.2',
    'choices': [
        {
            'index': 0,
            'message': {'role': 'assistant', 'content': 'golden reply'},
            'finish_reason': 'stop',
        }
    ],
    'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
}


class _Recorder:
    """记录发往上游的请求（path + JSON body），并按 fixture 回响应。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content.decode('utf-8'))
            except ValueError:
                body = request.content.decode('utf-8', errors='replace')
        self.calls.append({'path': request.url.path, 'body': body})
        if request.method == 'GET' and request.url.path.endswith('/v1/models'):
            return httpx.Response(200, json=UPSTREAM_MODELS_PAYLOAD)
        return httpx.Response(200, json=UPSTREAM_CHAT_PAYLOAD)

    def client(self, *args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


# ── 三个协议的入站请求（合成 fixture，字段覆盖迁移会碰到的形状）──────────
CHAT_REQUEST: dict[str, Any] = {
    'model': 'glm-5.2',
    'messages': [
        {'role': 'system', 'content': 'You are a careful assistant.'},
        {'role': 'user', 'content': 'Summarize the migration plan in one line.'},
    ],
    'max_tokens': 256,
    'temperature': 0.2,
}

RESPONSES_REQUEST: dict[str, Any] = {
    'model': 'glm-5.2',
    'instructions': 'You are a careful assistant.',
    'input': 'Summarize the migration plan in one line.',
    'max_output_tokens': 256,
}

ANTHROPIC_REQUEST: dict[str, Any] = {
    'model': 'glm-5.2',
    'max_tokens': 256,
    'system': 'You are a careful assistant.',
    'messages': [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Summarize the migration plan in one line.'}]},
    ],
}

# 保留期默认值：直接读运行时真正使用的那几个常量（不复制字面量）。
RETENTION_SOURCES = (
    '_REQUEST_LOG_RETAIN_DAYS',
    '_REQUEST_LOG_CHECK_EVERY',
    '_HOURLY_RETAIN_DAYS',
    '_TASK_LOG_RETAIN_DAYS',
    '_AUDIT_LOG_MAX_ROWS',
)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


def _retention_defaults() -> dict[str, Any]:
    from server import db as dbmod

    values = {name: getattr(dbmod, name) for name in RETENTION_SOURCES}
    # `log_retention_overrides` 的契约默认值集中在 settings_defaults（单一事实来源）。
    from server.tests.settings_defaults import FLAG_DEFAULTS

    values['log_retention_overrides'] = FLAG_DEFAULTS['log_retention_overrides']
    return values


def build_payloads() -> dict[str, str]:
    """跑真实代码路径，返回 `{文件名: 归一化后的 JSON 文本}`。"""
    from starlette.testclient import TestClient

    from server.main import app

    recorder = _Recorder()
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    originals = {
        'DB_PATH': config.DB_PATH,
        'USERS_FILE': config.USERS_FILE,
        'STATIC_DIR': config.STATIC_DIR,
        'WB2API_BASE': config.WB2API_BASE,
        'WB2API_KEY': config.WB2API_KEY,
    }
    try:
        config.DB_PATH = root / 'golden.db'
        config.USERS_FILE = root / 'users.json'
        config.STATIC_DIR = root / 'no-static'
        # 出站 api_key 用合成值：固定下来才能让「发往上游的请求」逐字一致。
        config.WB2API_BASE = 'http://upstream.invalid'
        config.WB2API_KEY = FIXTURE_UPSTREAM_KEY
        config.USERS_FILE.write_text(
            json.dumps({'secret': 'S' * 64, 'users': [], 'api_keys': []}),
            encoding='utf-8',
        )
        db._conn = None
        db.connect()
        token = keysvc.create_key(name=FIXTURE_CLIENT_KEY_NAME)['key']
        headers = {'Authorization': f'Bearer {token}'}

        with mock.patch.object(config, 'http_client', side_effect=recorder.client):
            with TestClient(app) as client:
                chat = client.post('/v1/chat/completions', json=CHAT_REQUEST, headers=headers)
                responses = client.post('/v1/responses', json=RESPONSES_REQUEST, headers=headers)
                anthropic = client.post('/v1/messages', json=ANTHROPIC_REQUEST, headers=headers)
                models = client.get('/v1/models', headers=headers)
        for name, resp in (('chat', chat), ('responses', responses), ('anthropic', anthropic)):
            if resp.status_code != 200:
                raise AssertionError(f'{name} 协议采集失败：HTTP {resp.status_code} {resp.text[:300]}')
        if models.status_code != 200:
            raise AssertionError(f'/v1/models 采集失败：HTTP {models.status_code} {models.text[:300]}')

        # 三次 POST 依次对应三个协议（顺序由上面的调用顺序保证）。
        posts = [call for call in recorder.calls if call['path'].endswith('/v1/chat/completions')]
        if len(posts) != 3:
            raise AssertionError(f'预期 3 次上游 POST，实际 {len(posts)}：{recorder.calls}')
        return {
            'chat_request_body.json': _dumps(posts[0]['body']),
            'responses_request_body.json': _dumps(posts[1]['body']),
            'anthropic_request_body.json': _dumps(posts[2]['body']),
            'models_listing.json': _dumps(models.json()),
            'retention_defaults.json': _dumps(_retention_defaults()),
        }
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        for name, value in originals.items():
            setattr(config, name, value)
        try:
            tmp.cleanup()
        except PermissionError:
            pass


def write_payloads(target: Path | None = None) -> list[Path]:
    """把采集结果写到 `target`（默认 `server/tests/golden/`），返回写出的路径。"""
    out_dir = Path(target) if target is not None else GOLDEN_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in build_payloads().items():
        path = out_dir / name
        path.write_text(text, encoding='utf-8')
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    target = None
    if argv and not argv[0].startswith('-'):
        target = Path(argv.pop(0))
    for path in write_payloads(target):
        print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
