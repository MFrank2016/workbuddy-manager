"""模型路由的**保存校验**（与解析/清单分离；契约 §2）。

为什么单独一个模块：`modelroutes.py` 同时装着「旧表搬迁 + 6 级解析链 + 清单别名
+ 保存校验」四件事，早已越过单文件可维护的边界。搬迁与解析是**读**路径，
本模块是**写**路径 —— 它们的失败模式也不同：读路径要容错（坏数据只影响它自己），
写路径要给用户一句能读懂的拒绝理由。

**校验只有一份**：管理面（`services.modelrouteadmin`）与旧表搬迁共用这里的
`validate`，两处各写一份必然漂移（某天 `realm` 多一个合法值，只有一边认）。
"""
from __future__ import annotations

from typing import Any

from .. import db
from . import modelmap

_REALMS = ('', 'cn', 'global')


def _norm_flag(value: object, field: str) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ValueError(f'{field} 只能是 0 或 1')


def _check_realm_consistency(public_id: str, target: str, realm: str) -> None:
    """realm 列必须与**解析时会算出来的版本**一致，否则这行调用时只会被 400 打回。

    解析的 realm 只看模型名（`db.realm_of_model`，见 issue #47），因此：

      · 非 `*` 行：显式 realm 必须等于 public_id 的版本 —— `alias`（无前缀）配
        `realm='global'` 是**不可达**的（解析会用 cn 去找 global 行）；
      · `*` 是兜底**键**、不是模型名，realm 由调用方的模型名决定，所以不受上一条
        约束（否则「未知模型一律走国际版」这种合法意图就无法表达）；
      · 显式 realm 还必须等于**目标模型**的版本，否则命中后立刻 400。

    realm='' 的行对两个版本都适用，目标可指向任一版本（升级窗口从旧 `model_map`
    搬来的 `alias → global:x` 正是这种形状，issue #47）。它是否出现在某把密钥的
    清单里，由清单侧按**目标版本**过滤，所以这里不拦。
    """
    if not realm:
        return
    if public_id != modelmap.FALLBACK_KEY:
        name_realm = db.realm_of_model(public_id)
        if name_realm != realm:
            raise ValueError(
                f'realm={realm!r} 与 public_id={public_id!r} 的版本不一致'
                f'（按模型名判定为 {name_realm!r}）：调用时该行不可达、必被版本'
                '检查打回。请给 public_id 加 global: 前缀，或把 realm 留空')
    target_realm = db.realm_of_model(target)
    if target_realm != realm:
        raise ValueError(
            f'realm={realm!r} 与 target_model={target!r} 的版本不一致'
            f'（按模型名判定为 {target_realm!r}）：该行命中后必被版本检查打回。'
            '请改 realm，或换一个同版本的目标模型')


def _norm_row(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError('每条路由必须是一个对象')
    public_id = str(raw.get('public_id') or '').strip()
    target = str(raw.get('target_model') or '').strip()
    if not public_id:
        raise ValueError('public_id 不能为空')
    if not target:
        raise ValueError('target_model 不能为空')
    if target == modelmap.FALLBACK_KEY:
        raise ValueError('target_model 不能是兜底键 *（它是键，不是模型名）')
    realm = str(raw.get('realm') or '').strip().lower()
    if realm not in _REALMS:
        raise ValueError("realm 只能是 '' / 'cn' / 'global'")
    _check_realm_consistency(public_id, target, realm)
    group = raw.get('upstream_group_id')
    if group is not None and (isinstance(group, bool) or not isinstance(group, int)
                             or group <= 0):
        raise ValueError('upstream_group_id 必须是正整数或留空')
    credits = raw.get('credits_override')
    if credits is not None:
        if isinstance(credits, bool) or not isinstance(credits, (int, float)):
            raise ValueError('credits_override 必须是非负数或留空')
        if credits < 0:
            raise ValueError('credits_override 必须是非负数或留空')
        credits = float(credits)
    return {
        'public_id': public_id,
        'target_model': target,
        'realm': realm,
        'upstream_group_id': group,
        'credits_override': credits,
        'keep_original': _norm_flag(raw.get('keep_original', 1), 'keep_original'),
        'enabled': _norm_flag(raw.get('enabled', 1), 'enabled'),
    }


def validate(payload: object) -> list[dict[str, Any]]:
    """校验并归一化要落库的路由；非法值或重复键抛 `ValueError`。

    重复键的口径必须与唯一索引一致：`(public_id, realm, COALESCE(group, 0))`。
    这里先拦一次，是为了给用户一句能读懂的报错；绕过它直接 INSERT 时由索引兜底
    抛 `sqlite3.IntegrityError`（两道防线，口径相同）。
    """
    rows = payload if isinstance(payload, list) else [payload]
    out = [_norm_row(row) for row in rows]
    seen: set[tuple[str, str, int]] = set()
    for row in out:
        key = (row['public_id'], row['realm'], row['upstream_group_id'] or 0)
        if key in seen:
            raise ValueError(
                f"路由重复：public_id={row['public_id']} realm={row['realm']!r} "
                f"group={row['upstream_group_id']}")
        seen.add(key)
    return out
