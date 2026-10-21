"""模型路由：旧 `model_map` 的幂等搬迁 + 6 级解析链 / 清单别名 / 保存校验（契约 §2）。

为什么需要这一层：旧设置 `model_map` 只能表达「别名 → 模型名」两列，而新的路由
表还要 realm / 上游分组 / `keep_original`。**升级不能要求用户重填配置**，所以
启动时把旧映射原样搬一次；搬完就靠 `settings['model_routes_backfill_v1']` 这个
marker 记住「已经搬过」，重复启动不再动它。

四条不能破的边界：

  * **绝不修改旧 `model_map`**：搬迁期间新表与旧表同时可用，旧表是唯一回退路径；
    搬完写一份快照到 `settings['model_map_pre_routes']` 供人工核对。
  * **marker 是唯一判据**：存在就 no-op；缺失就搬，已有的行用 `INSERT OR IGNORE`
    跳过。判据与插入必须在**同一个 `BEGIN IMMEDIATE` 事务**里，否则两处都是 TOCTOU。
  * **原子**：快照、全部行、marker 一次提交。失败整体回滚，下次启动重试全量。
  * **坏数据不阻断启动**：`model_map` 存成非对象（历史脏数据、手工改库）时按「没有
    映射」处理，只写 marker、不写行、不抛异常 —— 与 `modelmap.load` 同一原则。

6 级解析链（`resolve`）、清单别名（`aliases`）与保存校验（`validate`）在下面。
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .. import db
from . import modelmap

BACKFILL_MARKER = 'model_routes_backfill_v1'
SNAPSHOT_KEY = 'model_map_pre_routes'
MODEL_MAP_KEY = 'model_map'
_INSERT = (
    'INSERT OR IGNORE INTO model_routes(public_id, target_model, realm, upstream_group_id, '
    'credits_override, keep_original, enabled, created_at, updated_at) '
    "VALUES(?, ?, '', NULL, NULL, 1, 1, ?, ?)"
)
_PUT_SETTING = ('INSERT INTO settings(key, value) VALUES(?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value')


def _setting_on(conn: sqlite3.Connection, key: str) -> Any:
    """在**调用方的事务里**读设置。不能用 `db.get_setting`：它顺手 commit。"""
    row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(str(row['value']))
    except ValueError:
        return None


def backfill_model_routes() -> int:
    """幂等、原子、可续跑的回填。返回本次实际写入的行数。

    旧实现「表非空就返回」是致命伤：中途崩溃留下 1..N-1 行且无 marker，判据让
    它永远不再重试（`b` 与 `*` 兜底静默失效）。现在判据换成 marker，判据+快照+
    全部行+marker 在同一个 `BEGIN IMMEDIATE` 里，要么全成、要么全无；写锁也消掉了
    两进程同时启动的 TOCTOU。失败必须显式抛出。
    """
    # 便宜的快速路径：已经搬过就不必去抢写锁。真判断在事务内再做一次。
    if db.get_setting(BACKFILL_MARKER) is not None:
        return 0
    with db._lock:
        conn = db.connect()
        conn.execute('BEGIN IMMEDIATE')
        try:
            if _setting_on(conn, BACKFILL_MARKER) is not None:
                conn.commit()
                return 0
            model_map = _setting_on(conn, MODEL_MAP_KEY)
            snapshot = {} if model_map is None else model_map
            rows = [{'public_id': name, 'target_model': target}
                    for name, target in modelmap.load(snapshot).items()]
            now = int(time.time())
            before = conn.total_changes
            for row in rows:
                conn.execute(_INSERT, (row['public_id'], row['target_model'], now, now))
            moved = conn.total_changes - before
            conn.execute(
                _PUT_SETTING, (SNAPSHOT_KEY, json.dumps(snapshot, ensure_ascii=False)))
            conn.execute(_PUT_SETTING, (BACKFILL_MARKER, json.dumps(True)))
            conn.commit()
        except BaseException:
            # 回滚后原样抛出：调用方要看到真实错误。BaseException 是为了
            # KeyboardInterrupt 也先把事务收干净再往上抛。
            conn.rollback()
            raise
        return moved


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


# ── 6 级解析链 ──


@dataclass(frozen=True, slots=True)
class Alias:
    """一条可用于下游清单的路由行（`aliases` 的返回元素）。

    `ids` 是 `/v1/models` 要下发的 id 列表：`keep_original=1` 时上游原 ID 与别名
    都在（客户端发哪个都能调），`0` 时只下发别名。`credits_override` 仅供展示与
    排序，**不参与任何扣费**（见契约 §2）。
    """

    public_id: str
    target_model: str
    realm: str
    upstream_group_id: int | None
    credits_override: float | None
    keep_original: bool

    @property
    def ids(self) -> tuple[str, ...]:
        if not self.keep_original:
            return (self.public_id,)
        return (self.public_id, self.target_model)


_SELECT = (
    'SELECT public_id, target_model, realm, upstream_group_id, credits_override, '
    'keep_original FROM model_routes '
)


def _alias(row: Any) -> Alias:
    return Alias(
        public_id=str(row['public_id']),
        target_model=str(row['target_model']),
        realm=str(row['realm']),
        upstream_group_id=(
            None if row['upstream_group_id'] is None else int(row['upstream_group_id'])),
        credits_override=(
            None if row['credits_override'] is None else float(row['credits_override'])),
        keep_original=bool(row['keep_original']),
    )


def _row(public_id: str, realm: str, group: int | None) -> Alias | None:
    """查某一级的**唯一一行**（`enabled=1` 才算）。

    `group=None` 表示「任意分组」那一行（`upstream_group_id IS NULL`），而不是
    「不按分组过滤」—— 租户隔离靠的就是这两者严格区分：绑定组 2 的密钥
    永远查不到组 1 的行。唯一索引保证这一步至多一行，`LIMIT 1` 只是兜底。
    """
    sql = _SELECT + 'WHERE public_id = ? AND realm = ? AND enabled = 1 AND '
    args: list[Any] = [public_id, realm]
    if group is None:
        sql += 'upstream_group_id IS NULL'
    else:
        sql += 'upstream_group_id = ?'
        args.append(group)
    hit = db.query_one(sql + ' LIMIT 1', args)
    return _alias(hit) if hit else None


def _realm_order(realm: str) -> tuple[str, ...]:
    """先专属 realm，再空串（空串 = 两个版本都适用）。"""
    return (realm, '') if realm else ('',)


def _group_order(group: int | None) -> tuple[int | None, ...]:
    """先密钥自己的分组，再无分组行；未绑定的密钥只有无分组行参与。"""
    return (group, None) if group is not None else (None,)


def _pick(public_id: str, realm: str, group: int | None) -> Alias | None:
    """某个 public_id 在 4 个 realm/分组组合里的第一条命中。"""
    for candidate_realm in _realm_order(realm):
        for candidate_group in _group_order(group):
            hit = _row(public_id, candidate_realm, candidate_group)
            if hit:
                return hit
    return None


def _table_empty() -> bool:
    return db.query_one('SELECT 1 FROM model_routes LIMIT 1') is None


def resolve(model: str | None, realm: str, key_group: int | None) -> str | None:
    """下游模型名 → 实际要调用的上游模型名（契约 §2 的 6 级链）。

    顺序即契约，**不允许重排**：

      1. `public_id + realm + key_group`；2. 同 realm、无分组；
      3. `public_id + '' + key_group`；4. 同 `''`、无分组；
      5. 兜底 `*` 重复 1–4；6. 表为空 → 旧 `model_map`，否则原样透传。

    为什么第 6 级要区分「表为空」与「表非空但没命中」：表为空只出现在升级窗口
    （还没搬旧映射，或用户从未配过路由），此时必须沿用旧行为；一旦表里有行，
    用户配的就是完整意图，再回退旧表会让两套配置互相打架。

    `realm` 由调用方用 `db.realm_of_model(mapped or model)` 算好传进来 ——
    判定依据是**真正发往上游的那个名字**（issue #47）。
    """
    if not model:
        return model
    if _table_empty():
        return modelmap.target_for(db.get_setting('model_map', {}) or {}, model)
    realm = str(realm or '').strip().lower()
    for public_id in (model, modelmap.FALLBACK_KEY):
        hit = _pick(public_id, realm, key_group)
        if hit:
            return hit.target_model
    return model


def aliases(realm: str, key_group: int | None) -> list[Alias]:
    """`/v1/models` 要补的路由别名（不含 `*` 兜底行，按 public_id 排序）。

    每个 public_id 只出一条，且**用与 `resolve` 完全相同的优先级**挑那一行 ——
    否则清单会展示一个调用时不会命中的目标（用户按清单选模型，结果打错版本）。
    """
    realm = str(realm or '').strip().lower()
    sql = _SELECT + 'WHERE enabled = 1 AND public_id <> ?'
    args: list[Any] = [modelmap.FALLBACK_KEY]
    if key_group is None:
        sql += ' AND upstream_group_id IS NULL'
    else:
        sql += ' AND (upstream_group_id IS NULL OR upstream_group_id = ?)'
        args.append(key_group)
    public_ids = sorted({str(r['public_id']) for r in db.query(sql, args)})
    out: list[Alias] = []
    for public_id in public_ids:
        hit = _pick(public_id, realm, key_group)
        if hit:
            out.append(hit)
    return out


# ── 保存校验 ──

_REALMS = ('', 'cn', 'global')


def _norm_flag(value: object, field: str) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ValueError(f'{field} 只能是 0 或 1')


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
