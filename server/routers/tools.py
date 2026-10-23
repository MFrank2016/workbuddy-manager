"""tools.py — `/v1/search` 与 `/v1/mcp` 的 manager 代理（迁移 Todo 28）。

职责刻意只有三件：**鉴权**（全局 IP → 密钥 → 密钥状态/IP 关）、**专用限流**
（每下游密钥 60/min，与 chat 的 120/min 分开）、**转发**（按密钥绑定的上游转发，
并设置 `X-WB2A-Realm` / `X-WB2A-Profile`）。搜索逻辑一律在 core，manager 不实现
第二套（契约 §4）。

几处刻意的取舍：

  · **开关在鉴权之前**：`tools_search_enabled` / `tools_mcp_enabled` 关闭时回
    纯文本 404，与「路由未注册」不可区分 —— 不向未鉴权探测暴露端点存在性。
  · **全局 IP 规则先于密钥解析**：黑名单 IP 即使带无效/缺失密钥也回
    `403 ip_blocked`，且**不写 request_logs、不打上游**（与 `_authorize` 同源）。
  · **限流 429 只写入站 IP 日志**，不写 `request_logs`：被限的请求没有打到上游，
    把它记成一次上游调用会让账单/用量视图出现不存在的请求。
  · **头由 manager 设置、不透传客户端**：客户端伪造的 `X-WB2A-*` 一律被覆盖。
"""
from __future__ import annotations

import hashlib
import threading
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import config, db, iputil, keysvc, upstreamsvc
from .security import get_config as get_security_config
from .gateway import (_bearer, _log_ip, _oai_error, _record,
                      _upstream_headers, _upstream_for)

router = APIRouter()

# 每下游密钥每分钟的请求上限（契约 §4：manager 60/min、core 600/min）。
TOOLS_PER_MINUTE = 60

# 固定窗口限流：键 = SHA-256(密钥 id)（不存明文 id，也不信任客户端头）。
_windows: dict[str, tuple[int, int]] = {}
_windows_lock = threading.Lock()


def _rate_key(key: dict) -> str:
    return hashlib.sha256(str(key['id']).encode()).hexdigest()


def _allow(key: dict) -> bool:
    """固定 60s 窗口：窗口内第 N+1 次返回 False。多实例各自计数（契约接受近似）。"""
    bucket = _rate_key(key)
    now = int(time.time())
    with _windows_lock:
        start, count = _windows.get(bucket, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        count += 1
        _windows[bucket] = (start, count)
        return count <= TOOLS_PER_MINUTE


def _authorize_tools(request: Request) -> tuple[dict | None, str, str | None, JSONResponse | None]:
    """tools 入口的鉴权：全局 IP → 密钥 → `keysvc.validate_tools`。

    返回 `(key, ip, ua, error_response)`。顺序与 `gateway._authorize` 一致：
    全局 IP 规则**先于**密钥解析，所以黑名单 IP + 无效密钥也回 `ip_blocked`。
    """
    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')
    path = request.url.path

    # 1) 全局入站 IP 规则（与 _authorize 同源）。命中即 403，不写 request_logs。
    sec = get_security_config()
    if sec.get('enabled'):
        rules = [
            {'kind': r['kind'], 'cidr': r['cidr']}
            for r in db.query('SELECT kind, cidr FROM ip_rules')
        ]
        if not iputil.evaluate(ip, rules, sec.get('mode', 'blacklist')):
            _log_ip(ip, path, True, ua, 'ip_blocked')
            return None, ip, ua, _oai_error(
                f'来源 IP {ip} 被安全策略拦截', 403, 'permission_error', 'ip_blocked')

    # 2) 密钥解析
    token = _bearer(request)
    if not token:
        _log_ip(ip, path, True, ua, 'missing_key')
        return None, ip, ua, _oai_error(
            '缺少 API Key，请在 Authorization 头中提供 Bearer 令牌', 401,
            'authentication_error', 'missing_api_key')
    key = keysvc.resolve(token)
    if not key:
        _log_ip(ip, path, True, ua, 'invalid_key')
        return None, ip, ua, _oai_error('API Key 无效', 401, 'authentication_error', 'invalid_api_key')

    # 3) 密钥状态 + IP 关（跳过 realm / 模型白名单，见 validate_tools）
    reason = keysvc.validate_tools(key, ip)
    if reason:
        _log_ip(ip, path, True, ua, getattr(reason, 'code', 'forbidden'))
        return None, ip, ua, _oai_error(
            str(reason), getattr(reason, 'status', 403),
            getattr(reason, 'err_type', 'permission_error'),
            getattr(reason, 'code', 'forbidden'))
    return key, ip, ua, None


async def _proxy(request: Request, *, path: str, log_model: str) -> Response:
    """两个端点共用的代理流程。"""
    body = await request.body()
    key, ip, ua, err = _authorize_tools(request)
    if err is not None:
        return err

    if not _allow(key):
        # 只写入站 IP 日志：请求没打到上游，不该出现在 request_logs 里。
        _log_ip(ip, path, True, ua, 'rate_limit_exceeded')
        resp = _oai_error(
            'tools rate limit exceeded (60 requests/minute per api key)', 429,
            'rate_limit_exceeded', 'rate_limit_exceeded')
        resp.headers['Retry-After'] = '60'
        return resp

    upstream, up_err = _upstream_for(key, ip=ip, model=log_model, mapped=log_model, ua=ua)
    if up_err is not None:
        return up_err

    headers = _upstream_headers(upstream, key)
    url = str(upstream.get('base_url') or '').rstrip('/') + path
    started = time.monotonic()
    try:
        async with config.http_client(config.UPSTREAM_TIMEOUT, connect=5) as client:
            got = await client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        _record(key, ip, log_model, log_model, 502, 0, 0, elapsed, ua, str(exc), False)
        return _oai_error(f'上游不可达：{exc}', 502, 'upstream_error', 'upstream_unreachable')

    elapsed = int((time.monotonic() - started) * 1000)
    err_text = None
    if got.status_code >= 400:
        err_text = got.text[:500] if got.text else None
    # 0 token、credit NULL：tools 请求不产生模型用量（与 /v1/usage 同口径）。
    _record(key, ip, log_model, log_model, got.status_code, 0, 0, elapsed, ua, err_text, False)
    keysvc.touch(key, ip, tokens=0)

    content_type = got.headers.get('content-type', 'application/json')
    return Response(content=got.content, status_code=got.status_code, media_type=content_type)


@router.post('/v1/search')
async def search_proxy(request: Request) -> Response:
    """代理到 core 的 `/v1/search`；开关关闭时 404（与路由未注册不可区分）。"""
    if db.get_setting('tools_search_enabled', False) is not True:
        return JSONResponse({'detail': 'Not Found'}, status_code=404)
    return await _proxy(request, path='/v1/search', log_model='web_search')


@router.post('/v1/mcp')
async def mcp_proxy(request: Request) -> Response:
    """代理到 core 的 `/v1/mcp`；开关关闭时 404。"""
    if db.get_setting('tools_mcp_enabled', False) is not True:
        return JSONResponse({'detail': 'Not Found'}, status_code=404)
    return await _proxy(request, path='/v1/mcp', log_model='mcp')
