"""按**分组**拉 `/v1/models` 的单一实现（迁移 Todo 18）。

## 为什么单独一个模块

「用某个分组自己的地址与钥匙拉一次清单」被两处需要：管理端并集目录
（`catalogmerge`）与契约 §5 指定的公共入口 `modelcatalog.get_models_for_upstream`。
写在 `modelcatalog` 里会让两个模块互相 import（并集要用它的装饰与版本判据），
写两遍则是第二份事实。所以抽成这个**无依赖**的小模块，两边都单向依赖它。

## 失败即数据

返回 `(条目, 错误原因)` 而不是抛异常：调用方要按「这组挂了」降级，而不是让
一个分组的超时把整个目录打成 500。
"""
from __future__ import annotations

from .. import config, upstreamsvc
from .errtext import err_text

# 单个分组拉清单的超时：与 `wb2api.get_models` 同档（15s）。
TIMEOUT_SECONDS = 15


async def fetch_upstream_models(upstream: dict) -> tuple[list[dict], str]:
    """用**该分组自己的**地址与钥匙拉 `/v1/models`；返回 (原始条目, 错误原因)。

    返回原始条目而不是装饰后的：调用方要按 realm 过滤（`cn:` / `global:` 前缀
    只在原始形态里），装饰只该对最终清单做一次。
    """
    base = str((upstream or {}).get('base_url') or '').rstrip('/')
    if not base:
        return [], '该分组没有配置上游地址'
    key = upstreamsvc.forward_api_key(upstream or {})
    if not key:
        return [], '该分组没有 api_key，无法拉取模型清单'
    try:
        async with config.http_client(TIMEOUT_SECONDS, connect=3) as client:
            resp = await client.get(f'{base}/v1/models',
                                    headers={'Authorization': f'Bearer {key}'})
    except Exception as exc:  # noqa: BLE001
        return [], err_text(exc)
    if resp.status_code >= 400:
        return [], f'上游返回 {resp.status_code}'
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        return [], err_text(exc)
    items = body if isinstance(body, list) else (
        body.get('data') if isinstance(body, dict) else None
    )
    if not isinstance(items, list):
        return [], '上游返回的清单结构不可识别'
    return [m for m in items if isinstance(m, dict) and m.get('id')], ''
