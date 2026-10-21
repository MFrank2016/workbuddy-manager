"""保留策略表单纯逻辑的 Node 行为测试包装（`web/lib/retention-draft.ts`）。

为什么用 Python 包一层：本仓库的测试套件是 Python 的（`python -m unittest`），
而这段逻辑在前端。没有 Node（或版本太旧）时**跳过**而不是失败——与
`test_settings_tabs.py` / `test_command_palette.py` 同一处理。

被测的是三条「错了不报错、只是行为不对」的规则（详见 `retention-draft.ts` 顶部）：
稀疏提交、`ip_max_rows` 显式默认值仍提交、本地校验与服务端同边界。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'retention-draft.test.mjs'
_PURE = _ROOT / 'web' / 'lib' / 'retention-draft.ts'
_NODE = shutil.which('node')
_MIN_MAJOR = 22


def _node_major() -> int | None:
    if not _NODE:
        return None
    try:
        out = subprocess.run([_NODE, '--version'], capture_output=True,
                             text=True, timeout=15).stdout.strip()
        return int(out.lstrip('v').split('.')[0])
    except Exception:  # noqa: BLE001
        return None


_MAJOR = _node_major()


class RetentionDraftBehaviourTest(unittest.TestCase):
    """跑 Node 侧的行为测试：稀疏提交与边界校验。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_retention_draft_module(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')

        def run_once() -> subprocess.CompletedProcess:
            return subprocess.run(
                [_NODE, '--experimental-strip-types', str(_SCRIPT)],
                capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
            )

        proc = run_once()
        # 与 test_settings_tabs 同一处理：Node 偶发进程级崩溃（不是断言失败）重试一次；
        # rc=1（脚本自己判失败）不重试，免得守卫变成「跑两遍总能绿」。
        if proc.returncode not in (0, 1):
            proc = run_once()
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'保留策略表单逻辑不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class RetentionDraftInvariantTest(unittest.TestCase):
    """行为测试盖不到的结构前提。"""

    def test_module_stays_runnable_without_node_modules(self) -> None:
        """`retention-draft.ts` 不得出现**运行时** import。

        这是 Node 测试能跑起来的前提：`.mjs` 直接 `import './retention-draft.ts'`，
        而 Node 的 ESM 解析要求带扩展名，仓库的 app 代码却一律不写扩展名。一旦
        本模块 import 了别的模块的**值**，解析就会失败（症状是「Cannot find
        module」，看起来像路径写错）。
        """
        src = _PURE.read_text(encoding='utf-8')
        offenders = [
            line.strip() for line in src.splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(offenders, [], 'retention-draft.ts 出现了运行时 import：\n  '
                         + '\n  '.join(offenders))

    def test_bounds_match_backend(self) -> None:
        """前端边界必须与后端 `server/retention.py` 逐字一致。

        两边漂移的表现是：界面上填得进、提交后被 400 拒；或者反过来，界面提前
        拦下了一个后端其实接受的值。测试里**从后端读**边界，而不是再抄一份。
        """
        from server import retention as R

        src = _PURE.read_text(encoding='utf-8')
        # TS 里 `1_000_000` 与 Python 的 `1000000` 是同一个数，只是书写不同；
        # 比较时统一去掉下划线再拼字符串（否则这条守卫会被书写差异误伤）。
        def lit(n: int) -> str:
            return f'min: {n}, max: '

        self.assertIn(lit(R.DAYS_MIN) + str(R.DAYS_MAX), src.replace('_', ''),
                      '前端天数边界与后端 DAYS_MIN/DAYS_MAX 不一致')
        self.assertIn(lit(R.ROWS_MIN) + str(R.ROWS_MAX), src.replace('_', ''),
                      '前端行数边界与后端 ROWS_MIN/ROWS_MAX 不一致')

    def test_key_list_matches_backend(self) -> None:
        """五个键必须与后端 `retention.SPECS` 完全相同（顺序也一致）。"""
        from server import retention as R

        src = _PURE.read_text(encoding='utf-8')
        start = src.index('export const RETENTION_KEYS')
        end = src.index('] as const;', start)
        found = [line.strip().strip(",'") for line in src[start:end].splitlines()
                 if line.strip().startswith("'")]
        self.assertEqual(found, list(R.SPECS),
                         '前端的键清单与后端 retention.SPECS 漂移了')


if __name__ == '__main__':
    unittest.main()
