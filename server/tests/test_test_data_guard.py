"""数据目录保险的回归测试。

保险本身是「导入测试包时」执行的，所以这里测的是它拆出来的两个纯函数：
`_looks_like_real_data` 与 `_redirect_default_data_dir`，以及 `_guard` 在
显式真实目录 / 显式放行时的分支。导入期行为由 `server.tests.__init__` 自身
在每次跑测试时验证（能跑到这里就说明默认落点已被隔离）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config
from server.tests import (
    _AUTH_GLOB,
    _DATA_MARKERS,
    _guard,
    _looks_like_real_data,
    _redirect_default_data_dir,
)


class _TmpRepo:
    """临时造一个「仓库根 + data/」的部署现场。"""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / 'data'
        self.auth = self.data / 'auths'
        self.auth.mkdir(parents=True)
        (self.data / 'manager.db').write_bytes(b'not-a-real-db')
        (self.data / 'users.json').write_text('{"users": []}', encoding='utf-8')

    def close(self) -> None:
        self._tmp.cleanup()


class LooksLikeRealDataTest(unittest.TestCase):
    def test_data_markers(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(_looks_like_real_data(Path(d)))
            (Path(d) / _DATA_MARKERS[0]).write_bytes(b'x')
            self.assertTrue(_looks_like_real_data(Path(d)))

    def test_auth_glob(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(_looks_like_real_data(Path(d)))
            (Path(d) / _AUTH_GLOB.replace('*', '123')).write_text('{}', encoding='utf-8')
            self.assertTrue(_looks_like_real_data(Path(d)))


class RedirectDefaultDataDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _TmpRepo()
        self._orig = (config.ROOT, config.DATA_DIR, config.DB_PATH,
                      config.USERS_FILE, config.AUTH_DIR)
        config.ROOT = self.repo.root
        config.DATA_DIR = self.repo.data
        config.AUTH_DIR = self.repo.auth
        config.DB_PATH = self.repo.data / 'manager.db'
        config.USERS_FILE = self.repo.data / 'users.json'
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in ('WB_DATA_DIR', 'WB_AUTH_DIR', 'WB_DB', 'WB_USERS_FILE',
                    'WB_TEST_ALLOW_REAL_DATA'):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        self._env.stop()
        (config.ROOT, config.DATA_DIR, config.DB_PATH,
         config.USERS_FILE, config.AUTH_DIR) = self._orig
        self.repo.close()

    def test_default_real_data_is_redirected_to_temp(self) -> None:
        self.assertTrue(_redirect_default_data_dir(config))
        self.assertNotEqual(Path(config.DATA_DIR), self.repo.data)
        self.assertEqual(Path(config.DATA_DIR).parent, Path(tempfile.gettempdir()))
        self.assertTrue(Path(config.AUTH_DIR).is_dir())
        self.assertEqual(os.environ['WB_DATA_DIR'], str(config.DATA_DIR))
        self.assertEqual(os.environ['WB_AUTH_DIR'], str(config.AUTH_DIR))
        # 原部署目录没有被改动
        self.assertTrue((self.repo.data / 'manager.db').exists())
        self.assertTrue((self.repo.data / 'users.json').exists())

    def test_explicit_real_data_is_not_redirected(self) -> None:
        os.environ['WB_DATA_DIR'] = str(self.repo.data)
        self.assertFalse(_redirect_default_data_dir(config))
        with self.assertRaises(RuntimeError) as ctx:
            _guard()
        self.assertIn('拒绝在真实', str(ctx.exception))

    def test_allow_real_data_bypasses_guard(self) -> None:
        os.environ['WB_TEST_ALLOW_REAL_DATA'] = '1'
        _guard()  # 不抛异常
        self.assertFalse(_redirect_default_data_dir(config))

    def test_non_default_real_data_is_not_redirected(self) -> None:
        other = self.repo.root / 'elsewhere'
        other.mkdir()
        (other / 'manager.db').write_bytes(b'x')
        config.DATA_DIR = other
        self.assertFalse(_redirect_default_data_dir(config))


if __name__ == '__main__':
    unittest.main()
