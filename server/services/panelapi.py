"""core 内置面板 API 的薄客户端（manager 侧代理用）。

为什么单独一层：面板能力的路由很多，但「按分组解析实例 + Bearer 鉴权 + 把上游
错误原样映射成 manager 的 HTTP 错误」只有这一份判据。写在每个路由里，迟早出现
某个端点忘了带分组、或把上游 422 吞成 500。
"""
from __future__ import annotations

import asyncio

from .. import config, upstreamsvc


class PanelAPIError(Exception):
    """面板 API 调用失败；status 直接作为 manager 的 HTTP 状态码。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def resolve_upstream(upstream_id: int | None) -> dict:
    """解析目标分组：None/0 = 默认上游；显式 id 必须存在且启用。"""
    if not upstream_id:
        return upstreamsvc.default_upstream()
    up = upstreamsvc.get_upstream(upstream_id)
    if up is None:
        raise PanelAPIError(404, '分组不存在（可能已被删除）')
    if not up.get('enabled', True):
        raise PanelAPIError(409, '分组已停用，无法调用其面板 API')
    return up


async def call(upstream: dict, method: str, path: str,
               body: dict | None = None, timeout: float = 30.0) -> dict:
    """调用 core 的 `/panel/api/*`，返回解析后的 JSON。

    - 鉴权：`forward_api_key(upstream)`（默认分组 / 同址分组沿用默认 key）
    - 传输错误 → 502；上游 >=400 → 原状态码 + 上游 error/detail 原文
    - 非 JSON 成功响应 → 502（面板 API 约定返回 JSON）
    """
    base = str(upstream.get('base_url') or '').strip().rstrip('/')
    if not base:
        raise PanelAPIError(400, '分组未配置 base_url')
    key = upstreamsvc.forward_api_key(upstream)
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    try:
        async with config.http_client(timeout, connect=5) as client:
            resp = await client.request(method, base + path, headers=headers, json=body)
    except Exception as exc:  # noqa: BLE001 - 传输层错误统一转 502
        raise PanelAPIError(502, f'面板 API 不可达：{exc}') from exc

    if resp.status_code >= 400:
        detail = ''
        try:
            data = resp.json()
            if isinstance(data, dict):
                detail = str(data.get('error') or data.get('detail') or data)
            else:
                detail = str(data)
        except Exception:  # noqa: BLE001 - 非 JSON 错误体
            detail = (getattr(resp, 'text', '') or '')[:500]
        raise PanelAPIError(resp.status_code, detail or f'面板 API 返回 {resp.status_code}')

    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise PanelAPIError(502, f'面板 API 返回非 JSON：{exc}') from exc
    if not isinstance(data, dict):
        raise PanelAPIError(502, '面板 API 返回了非对象 JSON')
    return data


def call_sync_for_test(upstream: dict, method: str, path: str,
                       body: dict | None = None) -> dict:
    """测试辅助：同步桥。生产路由一律 `await call()`。"""
    return asyncio.run(call(upstream, method, path, body))
