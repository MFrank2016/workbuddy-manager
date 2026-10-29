"""identity 配置保存：默认映射合并、用户覆盖、enabled:false 保留。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server import config
from server.services import wb2api


class IdentityConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.cfg_path

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        self._tmp.cleanup()

    def _write(self, obj: dict) -> None:
        self.cfg_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')

    def _read(self) -> dict:
        return json.loads(self.cfg_path.read_text(encoding='utf-8'))

    def test_defaults_merge_and_user_override(self) -> None:
        self._write({'identity': {'enabled': False, 'replacements': {'Claude Code': 'MyBot'}}})
        wb2api.save_upstream_config({'identity': {'target': 'WorkBuddy'}})
        ident = self._read()['identity']
        self.assertFalse(ident['enabled'])
        self.assertEqual(ident['replacements']['Claude Code'], 'MyBot')
        self.assertEqual(ident['replacements']['workspace root'], 'workspace base')
        self.assertEqual(ident['replacements']['are paths the user'], 'are paths that the user')

    def test_rejects_non_string_replacement(self) -> None:
        self._write({'identity': {'enabled': True}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'identity': {'replacements': {'x': 1}}})

    def test_enabled_can_be_explicitly_changed(self) -> None:
        self._write({'identity': {'enabled': False}})
        wb2api.save_upstream_config({'identity': {'enabled': True}})
        self.assertTrue(self._read()['identity']['enabled'])
