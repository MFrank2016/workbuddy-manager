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
from . import modelmap, modelroutesvalidate

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


def _same_realm(alias: Alias, realm: str) -> bool:
    """这一行的**目标版本**是否与请求版本一致（`realm` 为空时无从判定，不拦）。"""
    return not realm or db.realm_of_model(alias.target_model) == realm


def _pick(public_id: str, realm: str, group: int | None) -> Alias | None:
    """某个 public_id 在 4 个 realm/分组组合里的第一条**同版本**命中。

    跨版本的行一律**跳过并继续下一级**（不 return）：命中它只会把请求改写成
    另一版本的模型，而调用方的版本检查必 400 `realm_mismatch`。这是 `'*'`
    兜底行与历史脏行的纵深防御——写入口径（`validate`）已经拦住了新行，但
    回填搬来的旧形状、手工改库的行仍可能跨版本。

    跳过而非透传是必要的：第 1 级跨版本时，第 2/3/4 级可能还有合法的同版本
    行（见 `test_cross_realm_row_falls_through_to_a_valid_lower_level`）。
    清单侧用同一个 `_pick`（`reachable`），所以「列出来的 id 都调得通」是
    按构造成立的，不靠两处各写一遍。
    """
    for candidate_realm in _realm_order(realm):
        for candidate_group in _group_order(group):
            hit = _row(public_id, candidate_realm, candidate_group)
            if hit and _same_realm(hit, realm):
                return hit
    return None


def _identity(alias: Alias) -> tuple[str, str, int | None]:
    """一行的身份（唯一索引那三列）；`credits_override` 只是展示值，不参与比对。"""
    return (alias.public_id, alias.realm, alias.upstream_group_id)


def reachable(alias: Alias, realm: str, key_group: int | None = None) -> bool:
    """这一行是否**真的**是 `resolve(alias.public_id, realm, key_group)` 会命中的那行。

    清单与解析必须共用这一个判据（issue #46 的「能选、一选就失败」）：调用侧
    实际用哪一行由 `_pick` 决定；清单若按别的口径（比如 `key.realm`）挑行，就会
    列出一个调用时根本不会命中的目标。
    """
    hit = _pick(alias.public_id, realm, key_group)
    return hit is not None and _identity(hit) == _identity(alias)


def _table_empty() -> bool:
    return db.query_one('SELECT 1 FROM model_routes LIMIT 1') is None


def _realm_of(model: str | None, mapping: object) -> str:
    """某个下游模型名解析时会用的 realm —— **与调用侧同源**（issue #47）。

    调用链是 `_map_model`：先按旧 `model_map` 试出候选名，再用**真正发往上游
    的那个名字**判版本，最后交给 `resolve`。清单必须用同一判据挑行，否则
    「按 `key.realm` 列出来的行」与「按模型名解析出来的行」会不一致。
    """
    return db.realm_of_model(modelmap.target_for(mapping, model) or model)


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

    **跨版本不改写**：`_pick` 只接受目标版本与 `realm` 一致的行，不一致就跳过、
    继续下一级；全都没命中就原样透传。所以 `'*'` 兜底只作用于**同版本**的模型，
    历史脏行（`realm=''` 却指向另一版本）也不会把请求改到另一套账号池。
    没有这条，清单列出的 id 会在调用时被版本检查 400 `realm_mismatch`。
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


def aliases(key_group: int | None) -> list[Alias]:
    """本分组可见的路由别名（不含 `*` 兜底行，按 public_id 排序）。

    每个 public_id 只出一条，且**用与 `resolve` 完全相同的优先级与 realm**
    挑那一行 —— realm 来自 `_realm_of(public_id, model_map)`（旧表优先，其次
    模型名），而不是调用方传进来的密钥版本。

    为什么不能按密钥版本挑：`resolve` 的 realm 只看模型名（issue #47），所以
    一行 `public_id='alias' + realm='global'` 对国际版密钥是**不可达**的（`alias`
    无前缀 → 解析用 cn）。旧实现按 `key.realm` 选行，于是把它列了出来、调用却
    必然 400 —— 正是 issue #46 的「能选、一选就失败」。现在这种行在
    `_pick(public_id, 'cn')` 里根本命中不了，自然不会被列出。

    密钥版本的过滤（「目标是另一版本」的别名）在 `services.modellisting`
    里做：那一维判的是**目标模型**的版本，与这里的可达性无关。
    """
    mapping = db.get_setting('model_map', {}) or {}
    sql = _SELECT + 'WHERE enabled = 1 AND public_id <> ?'
    args: list[Any] = [modelmap.FALLBACK_KEY]
    if key_group is None:
        sql += ' AND upstream_group_id IS NULL'
    else:
        sql += ' AND (upstream_group_id IS NULL OR upstream_group_id = ?)'
        args.append(key_group)
    out: list[Alias] = []
    for raw in db.query(sql, args):
        alias = _alias(raw)
        # 只列 `resolve` 真的会命中的那一行（同一判据 `reachable`）：
        # 每个 public_id 至多一条 —— 唯一索引 + `_pick` 只返回一个赢家。
        if reachable(alias, _realm_of(alias.public_id, mapping), key_group):
            out.append(alias)
    out.sort(key=lambda a: a.public_id)
    return out


# ── 保存校验 ──

# 校验在 `modelroutesvalidate`（写路径与读路径分开，见该模块的注释）；这里只做
# 转出，既有调用点（`modelrouteadmin`、测试）不必改 import 路径。
validate = modelroutesvalidate.validate
