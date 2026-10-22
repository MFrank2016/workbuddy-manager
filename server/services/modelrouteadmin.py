"""模型路由的管理面：面板 CRUD + 总开关（迁移契约 §2 / Todo 6）。

为什么单独一层：`server/routers/settings.py` 已经 400 行，而路由管理真正需要被
测的是**校验与错误分类**（400 / 404 / 409 各归其位），不是 HTTP 装饰器。把它们
放在服务层，路由层就只剩「校验 → 分类异常 → 写审计」三行。

三条边界：

  * **校验只有一份**：字段归一化（realm / bool / credits）复用
    `services.modelroutes.validate` —— 那是回填与保存共用的口径，这里再写一份
    必然漂移（比如某天 `realm` 多一个合法值，只有一边认）。
  * **分组必须存在且启用**：路由指向一个已删除/已停用的上游分组，等于把请求
    送到一个不存在的池子。这是**请求能改好**的问题（换一个分组或先启用它），
    所以归 400 而不是 404 —— 404 是「这个路由 id 不存在」，两者对用户的处置
    完全不同。
  * **唯一键口径与索引一致**：`(public_id, realm, COALESCE(group, 0))`。先查一次
    是为了给一句能读懂的报错，绕过时由索引兜底抛 `IntegrityError`，同样映射成
    409（两道防线、同一口径）。

`priority` 与账号绑定**不在这里**：契约 §2 的解析链是固定 6 级、每步至多一行，
没有优先级概念；路由也不绑定具体账号（那是密钥的 `upstream_id`）。所以本模块
的返回形状里不存在这两个字段，界面也就无从暴露。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from .. import db, upstreamsvc
from . import modelroutes

SETTING_KEY = 'model_routes_enabled'
_COLUMNS = ('id, public_id, target_model, realm, upstream_group_id, credits_override, '
            'keep_original, enabled, created_at, updated_at')
_BOOL_FIELDS = ('keep_original', 'enabled')


class DuplicateRoute(ValueError):
    """同一 `(public_id, realm, group)` 已有行 —— 映射成 409。"""


def _row(raw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {k: raw[k] for k in (
        'id', 'public_id', 'target_model', 'realm', 'upstream_group_id',
        'credits_override', 'created_at', 'updated_at')}
    for field in _BOOL_FIELDS:
        out[field] = bool(raw[field])
    return out


def _dup_key(public_id: str, realm: str, group: int | None) -> tuple[str, str, int]:
    """唯一键口径与索引一致：`(public_id, realm, COALESCE(group, 0))`。"""
    return (public_id, realm, group or 0)


def _ensure_unique(key: tuple[str, str, int], exclude_id: int | None = None) -> None:
    """先查一次，给用户一句能读懂的报错（唯一索引是第二道防线）。"""
    sql = ('SELECT id FROM model_routes WHERE public_id = ? AND realm = ? '
           'AND COALESCE(upstream_group_id, 0) = ?')
    args: list[Any] = [key[0], key[1], key[2]]
    if exclude_id is not None:
        sql += ' AND id <> ?'
        args.append(exclude_id)
    if db.query_one(sql, args) is not None:
        raise DuplicateRoute(
            f'路由重复：public_id={key[0]} realm={key[1]!r} group={key[2] or "任意"}')


def _ensure_group(group: int | None) -> None:
    """分组必须存在且启用（400：改请求就能成功）。"""
    if group is None:
        return
    upstream = upstreamsvc.get_upstream(group)
    if upstream is None:
        raise ValueError(f'上游分组不存在：{group}')
    if not upstream.get('enabled'):
        raise ValueError(f'上游分组已停用：{group}')


def list_routes() -> list[dict[str, Any]]:
    rows = db.query(
        f'SELECT {_COLUMNS} FROM model_routes '
        'ORDER BY public_id, realm, COALESCE(upstream_group_id, 0), id')
    return [_row(r) for r in rows]


def get_route(route_id: int) -> dict[str, Any] | None:
    raw = db.query_one(f'SELECT {_COLUMNS} FROM model_routes WHERE id = ?', (route_id,))
    return _row(raw) if raw else None


def create_route(payload: object) -> dict[str, Any]:
    clean = modelroutes.validate(payload)[0]
    _ensure_group(clean['upstream_group_id'])
    _ensure_unique(_dup_key(clean['public_id'], clean['realm'],
                            clean['upstream_group_id']))
    now = int(time.time())
    try:
        new_id = db.execute(
            'INSERT INTO model_routes(public_id, target_model, realm, upstream_group_id, '
            'credits_override, keep_original, enabled, created_at, updated_at) '
            'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (clean['public_id'], clean['target_model'], clean['realm'],
             clean['upstream_group_id'], clean['credits_override'],
             clean['keep_original'], clean['enabled'], now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise DuplicateRoute('同样的路由已经存在（public_id / realm / 分组 重复）') from exc
    created = get_route(new_id)
    if created is None:
        raise RuntimeError('插入后读不回刚写的路由')
    return created


def update_route(route_id: int, patch: dict[str, Any]) -> dict[str, Any] | None:
    """PATCH 语义：只改传进来的字段（未传的沿用原值后再统一校验）。"""
    current = get_route(route_id)
    if current is None:
        return None
    merged: dict[str, Any] = {k: current[k] for k in (
        'public_id', 'target_model', 'realm', 'upstream_group_id', 'credits_override',
        'keep_original', 'enabled')}
    for field in merged:
        if field in patch:
            merged[field] = patch[field]
    clean = modelroutes.validate(merged)[0]
    _ensure_group(clean['upstream_group_id'])
    _ensure_unique(
        _dup_key(clean['public_id'], clean['realm'], clean['upstream_group_id']),
        exclude_id=route_id)
    try:
        db.execute(
            'UPDATE model_routes SET public_id = ?, target_model = ?, realm = ?, '
            'upstream_group_id = ?, credits_override = ?, keep_original = ?, enabled = ?, '
            'updated_at = ? WHERE id = ?',
            (clean['public_id'], clean['target_model'], clean['realm'],
             clean['upstream_group_id'], clean['credits_override'], clean['keep_original'],
             clean['enabled'], int(time.time()), route_id),
        )
    except sqlite3.IntegrityError as exc:
        raise DuplicateRoute('同样的路由已经存在（public_id / realm / 分组 重复）') from exc
    return get_route(route_id)


def set_enabled(route_id: int, enabled: bool) -> dict[str, Any] | None:
    """单独开关一行（界面上的 switch）——只动 `enabled`，不重写其它字段。

    为什么不做成 `update_route(id, {'enabled': ...})` 的包装：那会重新校验整行，
    于是一条**历史脏数据**（比如分组后来被停用了）会让「关掉它」也失败。关掉一条
    路由永远应该成功。
    """
    if get_route(route_id) is None:
        return None
    db.execute('UPDATE model_routes SET enabled = ?, updated_at = ? WHERE id = ?',
               (1 if enabled else 0, int(time.time()), route_id))
    return get_route(route_id)


def delete_route(route_id: int) -> dict[str, Any] | None:
    """删除并返回被删的那行（审计要记它是什么）；不存在返回 None。"""
    current = get_route(route_id)
    if current is None:
        return None
    db.execute('DELETE FROM model_routes WHERE id = ?', (route_id,))
    return current


# ── 总开关 ──


def switch_enabled() -> bool:
    return bool(db.get_setting(SETTING_KEY, False))


def set_switch(enabled: bool) -> bool:
    """写总开关；返回新值。关掉时**不动表数据**——用户配的路由还在，只是不生效。"""
    db.set_setting(SETTING_KEY, bool(enabled))
    return bool(enabled)
