"""网关流式路径：上游在流中途断开时的收尾契约。

背景（2026-11-02 线上）：workbuddy2api 容器重建时，正在传输的 SSE 被从
中间掐断。manager 的 ``resp.aiter_bytes()`` 抛 ``httpx.RemoteProtocolError``
（incomplete chunked read），Starlette 直接把 ASGI 连接关掉——客户端只看到
chunked body 没有结束块，表现为裸 ``ECONNRESET``，而且请求日志仍记成 200。

这里钉住两件事：
1. 客户端必须收到一帧可读的 SSE error + ``[DONE]``，而不是裸连接重置；
2. 内部请求日志必须记 502 + 错误原因，不能把失败伪装成成功。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402


def _sse(obj) -> bytes:
    return ("data: " + json.dumps(obj) + "\n\n").encode()


class _Resp:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, chunks: list[bytes], exc: Exception | None = None) -> None:
        self._chunks = chunks
        self._exc = exc

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk
        if self._exc is not None:
            raise self._exc

    async def aclose(self) -> None:
        return None


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def build_request(self, *a, **kw):
        return object()

    async def send(self, req, stream=True):
        return self._resp

    async def aclose(self) -> None:
        return None


class StreamInterruptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / "s.db"
        config.USERS_FILE = Path(self._tmp.name) / "users.json"
        db._conn = None
        db.connect()
        security.save_users(
            {
                "secret": "S",
                "users": [
                    {
                        "username": "admin",
                        "role": "admin",
                        "pwd_hash": security.make_hash("p"),
                    }
                ],
                "api_keys": [],
            }
        )
        from server.main import app

        self.client = TestClient(app)
        self.token = keysvc.create_key("t")["key"]

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _chat(self, resp: _Resp):
        with mock.patch.object(config, "http_client", lambda *a, **k: _Client(resp)):
            return self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

    def _last_log(self) -> dict:
        return db.query_one(
            "SELECT status, error FROM request_logs ORDER BY id DESC LIMIT 1"
        )

    def test_midstream_transport_error_becomes_sse_error_and_done(self) -> None:
        partial = _sse(
            {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}
        )
        resp = _Resp(
            [partial],
            httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body "
                "(incomplete chunked read)"
            ),
        )

        r = self._chat(resp)

        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("partial", r.text)
        self.assertIn("upstream stream interrupted", r.text)
        self.assertIn("upstream_stream_interrupted", r.text)
        self.assertTrue(
            r.text.rstrip().endswith("data: [DONE]"),
            f"流没有以 [DONE] 收尾，客户端会当成连接重置：{r.text!r}",
        )

        row = self._last_log()
        self.assertEqual(
            row["status"], 502, "HTTP 头已发出仍是 200，但内部日志必须记 502"
        )
        self.assertIn("RemoteProtocolError", row["error"] or "")

    def test_clean_stream_is_unchanged(self) -> None:
        body = (
            _sse({"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]})
            + b"data: [DONE]\n\n"
        )
        r = self._chat(_Resp([body]))

        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("hello", r.text)
        self.assertNotIn("upstream_stream_interrupted", r.text)
        self.assertEqual(self._last_log()["status"], 200)


if __name__ == "__main__":
    unittest.main()
