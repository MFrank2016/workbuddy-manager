"""模型可用性判定（前端逻辑，借 Node 执行）。

`modelAvailabilityOf` 错起来**界面不报错**，只是徽标凭空出现或消失：不剥
`global:` 前缀时国际版模型永远匹配不到锁；忽略 realm 会把国内版的锁串到国际版；
取「第一条」而不是「最新一条」最近事件会让恢复/失败状态滞后。这些都不会抛异常，
只会让用户看到一个**看起来很正常**的错徽标。

为什么用 Python 包一层：本仓库的测试套件是 Python 的，而这段逻辑在前端。
没有 Node（或版本太旧）时**跳过**而不是失败——后端开发者不该因为机器上没装
Node 就跑不了整个测试套件。

跑的是 `web/lib/model-availability.test.mjs`，它直接 import `.ts` 源码（Node ≥ 22.6
的 type stripping），因此测的是**真实实现**。下面有一条断言钉住「本模块没有运行时
import」这个前提：破了之后表现为「找不到模块」，很容易被误判成路径写错。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'model-availability.test.mjs'
_PURE = _ROOT / 'web' / 'lib' / 'model-availability.ts'
_NODE = shutil.which('node')

# 只跑 .mjs，不碰 .ts —— Node 的 type stripping 从 22.6 起才有
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


def _code(path: Path) -> str:
    """读出源码并剥掉注释（注释里正解释着「不要这么写」，不剥会误判）。"""
    src = path.read_text(encoding='utf-8')
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    return re.sub(r'//[^\n]*', '', src)


class ModelAvailabilityBehaviourTest(unittest.TestCase):
    """跑 Node 侧的行为测试：锁池命中、跨域、最新事件与前缀剥离的真实实现。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_model_availability(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')

        def run_once() -> subprocess.CompletedProcess:
            return subprocess.run(
                [_NODE, '--experimental-strip-types', '--test', str(_SCRIPT)],
                capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
            )

        proc = run_once()
        # Node 偶发**进程级崩溃**（整套测试并跑时出现过），既不是断言失败、也与
        # 被测逻辑无关，却会把整批测试染红。只对「异常退出码」重试一次；脚本
        # 自己判失败（rc=1）不重试 —— 否则这条守卫就变成「跑两遍总能绿」的空转。
        if proc.returncode not in (0, 1):
            proc = run_once()
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'模型可用性判定不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class ModelAvailabilityInvariantTest(unittest.TestCase):
    """行为测试盖不到的前提：模块必须能被 Node 直接 import。"""

    def test_module_stays_runnable_without_node_modules(self) -> None:
        """`model-availability.ts` 不得出现**运行时** import。

        `.mjs` 直接 import `.ts`，Node 的 ESM 解析**要求带扩展名**，而 app 代码
        一律不写扩展名。一旦 import 了别的模块的**值**，解析就会失败——症状是
        「Cannot find module」，看起来像路径写错。`import type` 不算（编译后消失）。
        """
        offenders = [
            line.strip() for line in _code(_PURE).splitlines()
            if line.strip().startswith('import ')
            and not line.strip().startswith('import type')
        ]
        self.assertEqual(
            offenders, [],
            'model-availability.ts 出现了运行时 import：\n  ' + '\n  '.join(offenders)
            + '\n本模块被 web/lib/model-availability.test.mjs 直接 import，引入运行时 '
              'import 会让那条测试以「找不到模块」失败。',
        )


if __name__ == '__main__':
    unittest.main()
