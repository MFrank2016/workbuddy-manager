"""模型路由的建表迁移：把旧 `model_map` 幂等地搬进 `model_routes`（契约 §2）。

为什么需要这一层：旧设置 `model_map` 只能表达「别名 → 模型名」两列，而新的路由
表还要 realm / 上游分组 / `keep_original`。**升级不能要求用户重填配置**，所以
启动时把旧映射原样搬一次；搬完就靠 `settings['model_routes_backfill_v1']` 这个
marker 记住「已经搬过」，重复启动不再动它。

三条不能破的边界：

  * **绝不修改旧 `model_map`**：搬迁期间新表与旧表同时可用，旧表是唯一回退路径；
    搬完写一份快照到 `settings['model_map_pre_routes']` 供人工核对。
  * **marker 先决条件**：marker 缺失 **且** 表为空才搬。表里已有行（上一版残留、
    用户手工建的）就不再搬，否则会撞唯一索引或产生重复别名。
  * **坏数据不阻断启动**：`model_map` 存成了非对象（历史脏数据、手工改库）时
    按「没有映射」处理，只写 marker、不写行、不抛异常 —— 与 `modelmap.load`
    同一原则：坏数据只该影响它自己。

6 级解析链（`resolve` / `aliases` / `validate`）不在这里，见 Todo 4。
"""
from __future__ import annotations

import time
from typing import Any

from .. import db
from . import modelmap

BACKFILL_MARKER = 'model_routes_backfill_v1'
SNAPSHOT_KEY = 'model_map_pre_routes'
_INSERT = (
    'INSERT INTO model_routes(public_id, target_model, realm, upstream_group_id, '
    'credits_override, keep_original, enabled, created_at, updated_at) '
    "VALUES(?, ?, '', NULL, NULL, 1, 1, ?, ?)"
)


def _rows() -> list[dict[str, Any]]:
    """待搬迁的行：每个非 `*` 别名一行，`*` 折成兜底行 `public_id='*'`。

    目标值空/键为空的条目由 `modelmap.load` 过滤掉（等于没写），所以这里拿到的
    一定是干净的 `{别名: 目标}`。
    """
    mapping = modelmap.load(db.get_setting('model_map', {}))
    return [
        {'public_id': name, 'target_model': target}
        for name, target in mapping.items()
    ]


def backfill_model_routes() -> int:
    """幂等回填。返回本次写入的行数（已搬过或表非空时为 0）。

    调用点：启动时（`main.lifespan`）与测试。失败必须显式抛出而不是静默 ——
    这是数据搬迁，悄悄半途而废会让用户看到「配了映射但不生效」。
    """
    if db.get_setting(BACKFILL_MARKER) is not None:
        return 0
    existing = db.query_one('SELECT 1 FROM model_routes LIMIT 1')
    if existing:
        return 0

    # 快照要在写 marker 之前落库：中途崩溃时下次仍会重跑，而快照是幂等覆盖写。
    db.set_setting(SNAPSHOT_KEY, db.get_setting('model_map', {}))
    now = int(time.time())
    rows = _rows()
    for row in rows:
        db.execute(_INSERT, (row['public_id'], row['target_model'], now, now))
    db.set_setting(BACKFILL_MARKER, True)
    return len(rows)


def rollback_model_routes() -> int:
    """契约 §2 的回滚：清空新表与两个 marker，旧 `model_map` 原样保留。

    返回删掉的路由行数。**不碰 `model_map`** —— 回滚的意义就是回到「只用旧表」，
    清掉它等于把用户的配置删了。
    """
    deleted = int(db.query_one('SELECT COUNT(*) AS n FROM model_routes')['n'])
    db.execute('DELETE FROM model_routes')
    db.execute(
        "DELETE FROM settings WHERE key IN (?, ?)", (BACKFILL_MARKER, SNAPSHOT_KEY))
    return deleted
