"""按上游分组并集模型目录（迁移 Todo 18，契约 §5 `catalog_group_merge`）。

## 这个模块管什么

多分组部署里，每个分组（上游实例）有自己的模型清单。管理端「模型中心」在
`catalog_group_merge=true` 时要看到**全部**分组的模型，并标明每个模型哪些分组
有它（`available_in_groups`）。

`/v1/models` **永不**经过这里：那把密钥只属于一个分组，列出别的分组的模型就是
issue #46 那类「能选、一选就失败」。并集是**管理端视角**，不是下游视角。

## 为什么单独一个模块

`services/modelcatalog.py` 已经负责「基础目录」（腾讯接口 → 默认上游回退），
再往里塞并集合并就会变成两件事。这里只**单向依赖**它的纯函数（`_decorate` /
`_strip_realm_prefix` / `_belongs`）与 `upstreammodels`；
`modelcatalog` 不 import 本模块，所以没有循环。

## 降级原则

某个分组拉不到清单（无 api_key / 超时 / HTTP 错误）只降级**该组**：其余分组
照常返回，原因进 `errors`（带分组名）。一个组挂掉就让整页变空，会让管理员
以为模型全没了 —— 那比少一个组的并集糟得多。
"""
from __future__ import annotations

from .. import upstreamsvc
from . import modelcatalog, upstreammodels


async def group_availability(realm: str) -> dict:
    """各模型分别被哪些**非默认**分组提供（管理端并集目录用）。

    为什么跳过默认分组（`id is None`）：基础目录本来就从默认分组（以及腾讯
    接口）来，把它写进 `available_in_groups` 只会得到一个 `null` 分组号。
    这里只回答「**另外**哪些分组也有它」。停用的分组也跳过 —— 它现在不服务
    任何请求，标「可用」会误导。

    返回 `{available_in_groups: {id: [分组 id…]}, models: {id: 原始条目},
    errors: [「分组名: 原因」…]}`。`models` 让调用方给并集里新增的模型补一条
    带元数据的条目（否则它们只能显示一个裸 id）。
    """
    merged: dict[str, list[int]] = {}
    raw_by_id: dict[str, dict] = {}
    errors: list[str] = []
    for group in upstreamsvc.list_upstreams():
        if group.get('id') is None or not group.get('enabled'):
            continue
        items, error = await upstreammodels.fetch_upstream_models(group)
        if error:
            errors.append(f'{group.get("name") or group.get("id")}: {error}')
            continue
        for item in items:
            if not modelcatalog._belongs(item, realm):
                continue
            # 前缀去掉后的名字才是清单里的 id；前缀本身只用于版本判定。
            bare = str(modelcatalog._strip_realm_prefix(item).get('id') or '')
            if not bare:
                continue
            bucket = merged.setdefault(bare, [])
            if group['id'] not in bucket:
                bucket.append(int(group['id']))
            raw_by_id.setdefault(bare, item)
    return {'available_in_groups': merged, 'models': raw_by_id, 'errors': errors}


async def merged_catalog(realm: str, base_models: list[dict]) -> dict:
    """在基础目录上叠加各分组的可用性（**仅管理端**，flag 开时调用）。

    基础目录（腾讯接口或默认上游）的条目原样保留，只补
    `available_in_groups`；并集里新增的模型按基础条目的同一套 `_decorate`
    规则补元数据，然后追加在后面（保持「已有条目在前」的稳定顺序）。
    """
    availability = await group_availability(realm)
    out: dict[str, dict] = {str(m.get('id') or ''): dict(m) for m in base_models}
    for mid, groups in availability['available_in_groups'].items():
        if mid in out:
            out[mid]['available_in_groups'] = groups
            continue
        decorated = modelcatalog._decorate([availability['models'][mid]], realm)
        if not decorated:
            continue
        entry = decorated[0]
        entry['available_in_groups'] = groups
        out[mid] = entry
    return {'models': list(out.values()), 'group_errors': availability['errors']}
