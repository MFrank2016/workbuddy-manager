"""`model_routes` 建表、幂等回填与回滚（迁移契约 §2）。

为什么要有这张表：旧 `model_map` 只能表达「别名 → 模型名」两列，无法表达
realm、上游分组、`keep_original` 这些新维度；而**升级不能要求用户重填配置**，
所以旧映射必须原样搬进新表一次。

本文件钉住的是搬迁的边界条件：
  1. 表结构就是契约那 10 列，唯一索引按 `(public_id, realm, COALESCE(group,0))`；
  2. 行数 = 非 `*` 条目数 +（有 `*` ? 1 : 0），`*` 变成兜底行；
  3. 重复启动行数不变（marker 存在就不再搬）；**marker 才是唯一判据** ——
     marker 缺失时即使表里已有行也要补齐缺的那些，半成品必须能收敛；
  4. 空 map / 非 dict 都只写 marker、不写行、**不抛异常**；
  5. 旧 `model_map` 与快照都在，回滚只删新表与两个 marker；
  6. 搬迁是**一个事务**：任一行插入失败 -> 0 行、无 marker、无快照，下次启动重试全量。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.services import modelroutes  # noqa: E402

COLUMNS = [
    'id', 'public_id', 'target_model', 'realm', 'upstream_group_id',
    'credits_override', 'keep_original', 'enabled', 'created_at', 'updated_at',
]
MAP = {'gpt-4o-mini': 'deepseek-v4-flash', '*': 'glm-5.2'}
# 三条：`a` 先插、`b` 第二（失败注入点）、`*` 最后 —— 用来证明第 2 条失败时
# 第 1 条也被回滚。
MAP3 = {'a': 'A', 'b': 'B', '*': 'Z'}
INSERT = (
    'INSERT INTO model_routes(public_id, target_model, realm, upstream_group_id, '
    'credits_override, keep_original, enabled, created_at, updated_at) '
    "VALUES(?, ?, '', NULL, NULL, 1, 1, 1, 1)"
)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'routes.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        finally:
            db._conn = None
            config.DB_PATH = self._orig
            self._tmp.cleanup()

    def _rows(self) -> list[sqlite3.Row]:
        return db.query('SELECT * FROM model_routes ORDER BY id ASC')

    def _count(self) -> int:
        return int(db.query_one('SELECT COUNT(*) AS n FROM model_routes')['n'])

    def _has_setting(self, key: str) -> bool:
        return db.query_one('SELECT 1 FROM settings WHERE key = ?', (key,)) is not None


class SchemaTest(_Case):
    def test_table_has_the_ten_contract_columns(self) -> None:
        names = [str(r['name']) for r in db.query('PRAGMA table_info(model_routes)')]
        self.assertEqual(names, COLUMNS)

    def test_unique_index_uses_coalesced_group(self) -> None:
        row = db.query_one(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_model_routes'")
        self.assertIsNotNone(row, '唯一索引 ux_model_routes 不存在')
        sql = str(row['sql']).lower()
        self.assertIn('unique', sql)
        self.assertIn('coalesce(upstream_group_id, 0)', sql)

    def test_migrate_recreates_table_for_existing_database(self) -> None:
        """存量库升级路径：SCHEMA 之外的 `_migrate` 也要能补出这张表。"""
        db.execute('DROP TABLE model_routes')
        db._migrate(db._conn)
        names = [str(r['name']) for r in db.query('PRAGMA table_info(model_routes)')]
        self.assertEqual(names, COLUMNS)
        self.assertIsNotNone(db.query_one(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_model_routes'"))


class BackfillTest(_Case):
    def test_alias_and_fallback_become_two_rows(self) -> None:
        db.set_setting('model_map', MAP)

        moved = modelroutes.backfill_model_routes()

        self.assertEqual(moved, 2)
        rows = self._rows()
        self.assertEqual([str(r['public_id']) for r in rows], ['gpt-4o-mini', '*'])
        alias = rows[0]
        self.assertEqual(str(alias['target_model']), 'deepseek-v4-flash')
        self.assertEqual(str(alias['realm']), '')
        self.assertIsNone(alias['upstream_group_id'])
        self.assertIsNone(alias['credits_override'])
        self.assertEqual(int(alias['keep_original']), 1)
        self.assertEqual(int(alias['enabled']), 1)
        self.assertGreater(int(alias['created_at']), 0)
        self.assertEqual(int(alias['created_at']), int(alias['updated_at']))
        self.assertEqual(str(rows[1]['target_model']), 'glm-5.2')

    def test_rerun_keeps_row_count(self) -> None:
        db.set_setting('model_map', MAP)
        self.assertEqual(modelroutes.backfill_model_routes(), 2)

        self.assertEqual(modelroutes.backfill_model_routes(), 0)
        self.assertEqual(self._count(), 2)

    def test_map_without_fallback_adds_no_extra_row(self) -> None:
        db.set_setting('model_map', {'gpt-4o-mini': 'deepseek-v4-flash'})

        self.assertEqual(modelroutes.backfill_model_routes(), 1)
        self.assertEqual(self._count(), 1)

    def test_empty_map_writes_marker_only(self) -> None:
        db.set_setting('model_map', {})

        self.assertEqual(modelroutes.backfill_model_routes(), 0)
        self.assertEqual(self._count(), 0)
        self.assertTrue(self._has_setting('model_routes_backfill_v1'))

    def test_non_dict_model_map_does_not_raise(self) -> None:
        db.set_setting('model_map', ['not', 'a', 'dict'])

        moved = modelroutes.backfill_model_routes()

        self.assertEqual(moved, 0)
        self.assertEqual(self._count(), 0)
        self.assertTrue(self._has_setting('model_routes_backfill_v1'))

    def test_snapshot_equals_original_model_map(self) -> None:
        db.set_setting('model_map', MAP)

        modelroutes.backfill_model_routes()

        self.assertEqual(db.get_setting('model_map_pre_routes'), MAP)

    def test_old_model_map_is_never_modified(self) -> None:
        db.set_setting('model_map', MAP)

        modelroutes.backfill_model_routes()

        self.assertEqual(db.get_setting('model_map'), MAP)

    def test_preexisting_partial_state_converges_without_marker(self) -> None:
        """上一版/崩溃留下的半成品（有行、无 marker）必须补齐，而不是永远跳过。

        `a` 已存在且值是**错的**（`WRONG`）：收敛只补缺的行，不覆盖已有行 ——
        已有的行可能是用户手工改过的，回填无权拿旧 `model_map` 去盖掉它。
        """
        db.set_setting('model_map', MAP3)
        db.execute(INSERT, ('a', 'WRONG'))

        moved = modelroutes.backfill_model_routes()

        self.assertEqual(moved, 2)
        self.assertEqual(
            [(str(r['public_id']), str(r['target_model'])) for r in self._rows()],
            [('a', 'WRONG'), ('b', 'B'), ('*', 'Z')])
        self.assertTrue(self._has_setting('model_routes_backfill_v1'))

    def test_marker_present_is_a_noop_even_after_rows_were_deleted(self) -> None:
        """marker 是权威判据：搬过就不再搬，用户事后删行是他的意图。

        `rollback_model_routes` 是唯一的重搬入口（它会清掉 marker）。
        """
        db.set_setting('model_map', MAP)
        self.assertEqual(modelroutes.backfill_model_routes(), 2)
        db.execute("DELETE FROM model_routes WHERE public_id = '*'")

        self.assertEqual(modelroutes.backfill_model_routes(), 0)
        self.assertEqual(self._count(), 1)
        self.assertTrue(self._has_setting('model_routes_backfill_v1'))


class RollbackTest(_Case):
    def test_rollback_clears_routes_and_markers_but_keeps_model_map(self) -> None:
        db.set_setting('model_map', MAP)
        modelroutes.backfill_model_routes()

        deleted = modelroutes.rollback_model_routes()

        self.assertEqual(deleted, 2)
        self.assertEqual(self._count(), 0)
        self.assertFalse(self._has_setting('model_routes_backfill_v1'))
        self.assertFalse(self._has_setting('model_map_pre_routes'))
        self.assertEqual(db.get_setting('model_map'), MAP)

    def test_rollback_then_backfill_migrates_again(self) -> None:
        db.set_setting('model_map', MAP)
        modelroutes.backfill_model_routes()
        modelroutes.rollback_model_routes()

        self.assertEqual(modelroutes.backfill_model_routes(), 2)
        self.assertEqual(self._count(), 2)


class AtomicityTest(_Case):
    """回填必须是**一个事务**：失败不留半成品，下次启动能全量重试。

    注入方式用真实 SQLite 触发器（而不是 mock 掉 db.execute）：失败发生在
    真的 INSERT 语句里、真的 `RAISE(ABORT)`，所以它证明的是事务边界本身，
    而不是“我们记得在异常里调了 rollback”。
    """

    def _fail_on(self, public_id: str) -> None:
        # 触发器里不能绑变量，只能把 public_id 当字面量写进 DDL；值来自测试常量，
        # 但依旧转义单引号，避免以后有人拿它拼外部输入。
        literal = "'" + public_id.replace("'", "''") + "'"
        db.execute(
            'CREATE TRIGGER t_inject_failure BEFORE INSERT ON model_routes '
            f'WHEN NEW.public_id = {literal} '
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END",
        )

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.execute('DROP TRIGGER IF EXISTS t_inject_failure')
                db._conn.commit()
        finally:
            super().tearDown()

    def test_failure_on_second_insert_rolls_back_everything(self) -> None:
        db.set_setting('model_map', MAP3)
        self._fail_on('b')

        with self.assertRaises(sqlite3.Error) as caught:
            modelroutes.backfill_model_routes()

        self.assertIn('injected failure', str(caught.exception))
        self.assertEqual(self._rows(), [], '第 1 条也必须被回滚')
        self.assertFalse(self._has_setting('model_routes_backfill_v1'))
        self.assertFalse(self._has_setting('model_map_pre_routes'))

    def test_retry_after_failure_migrates_the_full_set(self) -> None:
        db.set_setting('model_map', MAP3)
        self._fail_on('b')
        with self.assertRaises(sqlite3.Error):
            modelroutes.backfill_model_routes()
        db.execute('DROP TRIGGER t_inject_failure')

        moved = modelroutes.backfill_model_routes()

        self.assertEqual(moved, 3)
        self.assertEqual(
            [str(r['public_id']) for r in self._rows()], ['a', 'b', '*'])
        self.assertTrue(self._has_setting('model_routes_backfill_v1'))

    def test_failure_does_not_poison_the_connection(self) -> None:
        """事务失败后连接必须还能用（没留下悬空的写事务）。"""
        db.set_setting('model_map', MAP3)
        self._fail_on('b')
        with self.assertRaises(sqlite3.Error):
            modelroutes.backfill_model_routes()

        self.assertEqual(self._count(), 0)
        self.assertEqual(db.get_setting('model_map'), MAP3)


class UniqueConstraintTest(_Case):
    def test_duplicate_public_id_realm_and_null_group_raises(self) -> None:
        sql = ('INSERT INTO model_routes(public_id, target_model, realm, upstream_group_id, '
               'credits_override, keep_original, enabled, created_at, updated_at) '
               'VALUES(?, ?, ?, ?, NULL, 1, 1, 1, 1)')
        db.execute(sql, ('m', 'a', '', None))

        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(sql, ('m', 'b', '', None))

    def test_same_public_id_in_another_realm_is_allowed(self) -> None:
        sql = ('INSERT INTO model_routes(public_id, target_model, realm, upstream_group_id, '
               'credits_override, keep_original, enabled, created_at, updated_at) '
               'VALUES(?, ?, ?, ?, NULL, 1, 1, 1, 1)')
        db.execute(sql, ('m', 'a', '', None))
        db.execute(sql, ('m', 'b', 'global', None))

        self.assertEqual(self._count(), 2)


if __name__ == '__main__':
    unittest.main()
