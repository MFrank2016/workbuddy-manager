"""日志保留策略的**可配置部分**（迁移 C2：`log_retention_overrides`）。

## 为什么是「稀疏 override」而不是「一份完整配置」

保留期以前是编译进 `server/db.py` 的常量。把它们整体搬到 settings 表听起来更
整齐，但会带来一个隐蔽的副作用：**存量部署从此被钉死在搬家的那一天**——以后
再调整默认常量（比如把审计上限从 2 万提到 5 万），所有已经部署过的实例都不再
跟着变，而用户从来没有表达过任何偏好。

所以只存**用户真正改过的键**。缺的键继续读常量，默认值只用于展示与校验：

    DEFAULTS = {request_days: 90, hourly_days: 90, task_days: 90,
                audit_max_rows: 20000, ip_max_rows: 20000}

## 为什么 `ip_max_rows` 显式等于默认值时**保留**

其余四个键写回默认值等于「撤销这条 override」，于是从库里删掉。`ip_max_rows`
不同：它决定 `db.add_ip_access_log` 走哪条裁剪路径——有 override 时每次写入都
精确删到 N 行（用户说「只留 20000 行」就该恰好 20000），没有时走旧的「每 500 次
检查、留 10% 余量」。显式写 20000 是在**选择精确语义**，不是「恢复默认」，所以
必须保留。

本模块刻意**不 import `db`**：`server/db.py` 要 import 它（`_prune_*` 读 override），
反过来就成了循环依赖。读写设置由调用方（db / 路由）负责，这里只做纯计算。
"""
from __future__ import annotations

from typing import Any

# 校验边界（与契约/前端输入框一致）。超出即 400，不做静默截断——把用户的
# 36500 悄悄改成 3650，界面上看起来保存成功了，实际是另一个值。
DAYS_MIN, DAYS_MAX = 1, 3650
ROWS_MIN, ROWS_MAX = 1, 1_000_000

# 键 → (单位, 默认值)。单位只用于校验边界与错误文案。
SPECS: dict[str, tuple[str, int]] = {
    'request_days': ('days', 90),
    'hourly_days': ('days', 90),
    'task_days': ('days', 90),
    'audit_max_rows': ('rows', 20000),
    'ip_max_rows': ('rows', 20000),
}

# 完整默认集：**只用于文档/UI 展示**，不写进库（见模块说明）。
DEFAULTS: dict[str, int] = {key: spec[1] for key, spec in SPECS.items()}

BOUNDS: dict[str, dict[str, int]] = {
    key: ({'min': DAYS_MIN, 'max': DAYS_MAX} if unit == 'days'
          else {'min': ROWS_MIN, 'max': ROWS_MAX})
    for key, (unit, _default) in SPECS.items()
}

# 显式等于默认值时仍要保留的键（它切换的是**行为路径**，不是数值）。
PRESERVE_AT_DEFAULT = frozenset({'ip_max_rows'})

SETTING_KEY = 'log_retention_overrides'


class InvalidRetention(ValueError):
    """校验失败。路由把它转成 400（带可读原因）。"""


def _bounds(key: str) -> tuple[int, int]:
    spec = SPECS[key]
    return ((DAYS_MIN, DAYS_MAX) if spec[0] == 'days' else (ROWS_MIN, ROWS_MAX))


def validate_value(key: object, value: object) -> int:
    """校验单个键值，返回规范化后的 int。

    拒绝 `bool` 要单独写：`True` 是 `int` 的子类，`isinstance(True, int)` 为真，
    于是 `{"task_days": true}` 会被当成 1 天——用户以为自己提交了非法值，却
    静默拿到了一个合法但完全不是他想要的结果。
    """
    if key not in SPECS:
        raise InvalidRetention(f'未知的保留项：{key!r}')
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRetention(f'{key} 必须是整数')
    low, high = _bounds(key)
    if not low <= value <= high:
        raise InvalidRetention(f'{key} 必须在 {low}–{high} 之间')
    return value


def merge(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, int]:
    """把一次保存合并进现有 override，返回**新的**稀疏对象。

    · 未知键 / 非法值 → `InvalidRetention`（整体拒绝，不部分落库）；
    · 值等于默认值且不在 `PRESERVE_AT_DEFAULT` 里 → 删除该键（撤销 override）；
    · 未在 patch 里出现的键 → 原样保留（PATCH 语义）。

    整体拒绝而不是「能存几个存几个」：部分成功会让用户面对一个「一半生效」的
    配置，而界面上只有一条报错，他不知道哪几项已经改了。
    """
    if not isinstance(patch, dict):
        raise InvalidRetention('请求体必须是对象')
    merged: dict[str, int] = {}
    for key, value in current.items():
        # 库里可能有历史/手工写入的脏值：能认就保留，认不出就丢弃（不阻断保存）。
        if key in SPECS:
            try:
                merged[key] = validate_value(key, value)
            except InvalidRetention:
                continue
    for key, value in patch.items():
        clean = validate_value(key, value)
        if clean == DEFAULTS[key] and key not in PRESERVE_AT_DEFAULT:
            merged.pop(key, None)
        else:
            merged[key] = clean
    return merged


def sanitize(overrides: dict[str, Any] | None) -> dict[str, int]:
    """只保留「认识的键 + 合法的值」。脏值直接丢弃（读路径不能因此报错）。

    库里可能有手工写入或历史版本留下的值：读路径不该因为它而 500，但更不能
    把非法值当成生效值（保留期 0 天 = 立刻删光全部日志）。
    """
    out: dict[str, int] = {}
    for key, value in (overrides or {}).items():
        if key not in SPECS:
            continue
        try:
            out[key] = validate_value(key, value)
        except InvalidRetention:
            continue
    return out


def effective(overrides: dict[str, Any] | None) -> dict[str, int]:
    """默认值 + override = 真正生效的值。脏值回落默认。"""
    out = dict(DEFAULTS)
    out.update(sanitize(overrides))
    return out


def limit(overrides: dict[str, Any] | None, key: str, fallback: int) -> int:
    """取某个键生效的值；没有合法 override 时返回 `fallback`（通常是模块常量）。

    这里刻意接受调用方传入**常量**而不是直接用 `DEFAULTS`：现有测试会
    `mock.patch.object(db, '_AUDIT_LOG_MAX_ROWS', 3)`，常量被 patch 后必须继续
    生效（否则「常量可被测试 patch」这条既有约定就断了）。
    """
    value = (overrides or {}).get(key)
    if value is None:
        return fallback
    try:
        return validate_value(key, value)
    except InvalidRetention:
        return fallback


def view(overrides: dict[str, Any] | None) -> dict[str, Any]:
    """GET/POST 的响应体：稀疏 override、完整默认集、生效值、校验边界。"""
    # 读路径只做「校验过滤」，**不**复用 merge：merge 会把「等于默认值且可撤销」
    # 的键删掉，于是显式 `ip_max_rows=20000` 在响应里消失、界面又跳回空值。
    clean = sanitize(overrides)
    return {
        'overrides': clean,
        'defaults': dict(DEFAULTS),
        'effective': effective(clean),
        'bounds': {k: dict(v) for k, v in BOUNDS.items()},
    }
