"""密钥 profile 选择器前端纯逻辑的 Node 行为测试包装（`web/lib/key-profile.ts`）。

与 `test_retention_draft.py` 同一处理：本仓库测试套件是 Python 的，而这段逻辑
在前端；没有 Node（或版本太旧）时**跳过**而不是失败。

被测的是「profile 与 realm 必须一致」这条 —— 它错了不会报错，只会让每次调用
打空池（见 server/services/keyprofiles.py 的说明）。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'key-profile.test.mjs'
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


class KeyProfileWebTest(unittest.TestCase):
    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_key_profile_module(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')

        def run_once() -> subprocess.CompletedProcess:
            return subprocess.run(
                [_NODE, '--experimental-strip-types', str(_SCRIPT)],
                capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
            )

        proc = run_once()
        if proc.returncode not in (0, 1):
            proc = run_once()
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'profile 表单逻辑不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class LocaleCoverageTest(unittest.TestCase):
    """五个语言都要有 keys.profile* 的文案（缺一条会在界面上显示成 key 本身）。"""

    def test_all_locales_define_profile_keys(self) -> None:
        import json

        base = _ROOT / 'web/lib/i18n/locales'
        required = ('profile', 'profileLimit', 'profileUnset', 'profileUnsetTitle',
                    'profileHintUnlimited', 'profileHintCnCli', 'profileHintCnWork',
                    'profileHintIntlCli', 'profileHintIntlWork', 'profileRealmConflict',
                    'profileAllowedMissing', 'colProfile')
        for f in sorted(base.glob('*.json')):
            data = json.loads(f.read_text(encoding='utf-8'))
            keys = data.get('keys', {})
            for key in required:
                self.assertIn(key, keys, f'{f.name} 缺 keys.{key}')
                self.assertTrue(str(keys[key]).strip(), f'{f.name} 的 keys.{key} 是空的')
