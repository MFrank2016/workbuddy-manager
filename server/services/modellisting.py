"""`/v1/models` 的路由别名与白名单裁剪（契约 §2）。

## 为什么单独一个模块

`/v1/models` 要回答的是「**这把密钥能用什么模型**」（issue #46），而接入路由表
之后它多了两维：**租户隔离**（只列本分组可见的路由）与 **`keep_original`**
（路由接管了上游原名时，那个名字还该不该出现在清单里）。这几条判据必须与
调用侧**同一口径**——列表里给出的每个 id，用同一把密钥去调用都必须被放行；
两处各写一遍迟早漂移，而漂移的表现正是「能选、一选就失败」。

`routers/gateway.py` 已经很大，所以这里只放清单侧的判据，路由层保留薄包装。

## 两条路径

  · **flag 关**：逐字沿用旧 `model_map` 的别名补条目（迁移前行为）；
  · **flag 开且表非空**：只认路由表；
  · **flag 开但表为空**：与 `modelroutes.resolve` 的第 6 级同口径，仍沿用旧表
    ——升级窗口里用户的映射不能凭空消失。
"""
from __future__ import annotations

from .. import db, keysvc
from . import modelmap, modelroutes


def routes_enabled() -> bool:
    """路由表总开关（契约 §5，默认关）。"""
    return bool(db.get_setting('model_routes_enabled', False))


def key_group(key: dict | None) -> int | None:
    """本次请求的租户分组：`key.upstream_id`；未绑定（含 `key=None`）为 NULL。

    契约 §2：路由只按 `(public_id, realm, key_group)` 匹配，未绑定密钥只吃
    NULL 组行 —— 这样就不存在「路由指向组 2 却打到组 1」的二义性。
    """
    raw = (key or {}).get('upstream_id')
    try:
        uid = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def listing_active() -> bool:
    """清单/别名补条目走路由表吗？（flag 开 **且** 表非空）"""
    if not routes_enabled():
        return False
    return db.query_one('SELECT 1 FROM model_routes LIMIT 1') is not None


def realm_of(model_id: object) -> str:
    """清单条目的版本：`global:` 前缀 = 国际版，其余 = 国内版。"""
    text = str(model_id or '').lower()
    return 'global' if text.startswith('global:') else 'cn'


def route_aliases(key: dict | None) -> list[modelroutes.Alias]:
    """本密钥可见的路由别名（flag 关时为空）。

    按 `key.upstream_id` 做租户隔离：`modelroutes.aliases` 只返回本组行与 NULL
    组行，其他分组的行对它**不可见**（契约 §2）。

    版本处理只剩**目标版本**一维：限定版本的密钥看不到指向另一版本的别名
    （空 realm 的行对两版都适用，但把指向 `global:` 的别名列给国内版密钥就是
    issue #46 的「能选、一选就失败」）。行本身是否可达、取哪一行，全部由
    `modelroutes.aliases` 用与 `resolve` 相同的判据决定 —— 这里不能再用
    `key.realm` 重挑一遍：解析的 realm 只看模型名，不看密钥版本。
    """
    if not routes_enabled():
        return []
    key = key or {}
    want = keysvc._norm_realm(key.get('realm'))
    group = key_group(key)
    # `modelroutes.aliases` 已用**与解析同一判据**（`reachable` + 模型名 realm）
    # 挑行，这里不再按 `key.realm` 重挑一遍 —— 那样正是把「不可达的行」
    # 列出来的根源（issue #46）。剩下的只有**目标版本**这一维：限定版本的
    # 密钥不能看到指向另一版本的别名（否则调用的版本检查必 400）。
    return [alias for alias in modelroutes.aliases(group)
            if not want or realm_of(alias.target_model) == want]


def _by_bare(items: list) -> dict[str, dict]:
    """`{裸 id: 清单条目}` —— 别名条目要借目标模型的元数据。"""
    out: dict[str, dict] = {}
    for item in items:
        if isinstance(item, dict) and item.get('id'):
            out.setdefault(keysvc._bare_model(item.get('id')), item)
    return out


def alias_entries(items: list, allow: list[str]) -> list[dict]:
    """旧 `model_map` 的白名单别名补条目（flag 关 / 路由表为空时使用）。

    白名单里写的是**别名**时，补一条以别名为 id 的模型条目（issue #46）。
    别名（设置页「模型映射」）是给下游用的名字，它**不在上游模型清单里**。
    若只按清单字面裁剪，白名单填成别名的密钥会拿到**空列表**——而那个别名明明
    能调用（鉴权判的是请求里的名字，模型映射发生在鉴权**之后**）。空列表比不裁
    更糟：客户端会显示「没有可用模型」，用户看不出是自己把名字写成了别名。

    条目取别名所指模型的信息，只把 `id` 换成别名——**那才是客户端要发的名字**
    （发清单里的真名会被白名单拒掉）。

    解析走 `modelmap.target_for`（含兜底，issue #173）：配了兜底模型之后，白名单里
    任意名字**都调得通**，那就不该只在没有兜底时补条目 —— 否则客户端拿到的清单里
    缺了那个名字，用户会以为"白名单没生效"，而其实是清单没列。
    """
    mapping = db.get_setting('model_map', {}) or {}
    if not isinstance(mapping, dict) or not mapping:
        return []
    by_bare = _by_bare(items)
    out: list[dict] = []
    seen: set[str] = set()
    for raw in allow:
        name = keysvc._bare_model(raw)
        # 本身就是清单里的模型 → 上面的白名单裁剪已保留它，不重复添加
        if not name or name in seen or name in by_bare:
            continue
        target = modelmap.target_for(mapping, name)
        src = by_bare.get(keysvc._bare_model(target)) if target else None
        if not src:
            continue          # 别名指向的模型不在本版本清单里 → 调不通，不给
        out.append({**src, 'id': name})
        seen.add(name)
    return out


def route_entries(items: list, aliases: list[modelroutes.Alias],
                  allow: list[str] | None) -> list[dict]:
    """把路由别名做成清单条目（`id` = 客户端要发的 public_id）。

    条目取**目标模型**的元数据，只把 `id` 换成 public_id —— 客户端按清单选模型，
    发的必须是能命中路由的那个名字。`allow=None` = 没有白名单，全部列出；否则只
    列白名单里写到的（白名单接受别名，见契约 §2）。
    """
    by_bare = _by_bare(items)
    wanted = None if allow is None else {keysvc._bare_model(x) for x in allow}
    out: list[dict] = []
    seen: set[str] = set()
    for alias in aliases:
        name = keysvc._bare_model(alias.public_id)
        if wanted is not None and name not in wanted:
            continue
        if not name or name in seen or name in by_bare:
            continue          # 本身就在清单里 → 不重复添加
        src = by_bare.get(keysvc._bare_model(alias.target_model))
        if not src:
            continue          # 目标不在本版本清单里 → 调不通，不给
        out.append({**src, 'id': alias.public_id})
        seen.add(name)
    return out


def _hidden_targets(routes: list[modelroutes.Alias]) -> set[str]:
    """`keep_original=0` 的路由所接管的上游原名（要从清单里撤下）。

    不撤的话客户端仍会选到那个名字：选了也能调（路由照旧生效），但用户会以为
    自己切回了上游原版，而实际走的是路由目标。
    """
    return {keysvc._bare_model(a.target_model) for a in routes if not a.keep_original}


def scope_models(payload: object, key: dict | None) -> object:
    """按密钥的**版本**与**模型白名单**裁剪模型列表，并补上路由别名。

    白名单判据与调用侧**共用 `keysvc.model_allowed`**，不另写一份：两处各写一遍
    迟早漂移，而漂移的表现正是 issue #46 报的现象。白名单为空 = 不限制。

    结构不是预期的 `{data: [...]}` 时**原样透传**——配额耗尽之类的判断
    不该因为我们认不出结构就去改动上游的响应。
    """
    key = key or {}
    want = keysvc._norm_realm(key.get('realm'))
    allow = [str(x) for x in (key.get('models') or []) if str(x).strip()]
    routes = route_aliases(key)
    if not want and not allow and not routes:
        return payload
    if not isinstance(payload, dict):
        return payload
    items = payload.get('data')
    if not isinstance(items, list):
        return payload

    in_realm = [m for m in items
                if isinstance(m, dict) and (not want or realm_of(m.get('id')) == want)]
    hidden = _hidden_targets(routes)
    visible = [m for m in in_realm if keysvc._bare_model(m.get('id')) not in hidden]
    if not allow:
        return {**payload, 'data': visible + route_entries(in_realm, routes, None)}

    scoped = [m for m in visible if keysvc.model_allowed(key, m.get('id'))]
    # 别名：白名单可能写的是下游熟悉的别名，它不在清单里，要补进来。
    # 旧 `model_map` 与路由表**不同时生效**（与 `resolve` 的第 6 级同口径）。
    if listing_active():
        scoped += route_entries(in_realm, routes, allow)
    else:
        scoped += alias_entries(in_realm, allow)
    return {**payload, 'data': scoped}

