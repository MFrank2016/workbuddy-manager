"""上游配置保存的三级生效路径（热生效 / 拒绝回滚 / 回退重启）。

背景：面板保存上游设置时直接写 `config.json`，而上游**只在启动时**读它 ——
所以每次改设置都得重启容器。容器刻意不挂 docker.sock（避免被攻破即取得宿主
root），于是面板只能弹一句「请在宿主机执行 docker compose restart」：

    配置已写入，但当前环境无法操作 docker，不会自动重启上游容器。

但上游自己带了一个热生效接口（`POST /panel/api/config`）：深合并 → 校验 → 写盘
→ 立即应用能热改的字段。所以面板应当**先走热生效**，只有上游不支持 / 调用失败
才回退重启。

这里钉住四条，做错了都不会报错、只会让用户白重启或让盘上留下非法配置：

  1. 热生效成功且无需重启 → 不提示重启、不调 docker、重载状态记为成功；
  2. 热生效成功但部分字段需重启 → 提示里**列出**这些字段；
  3. 上游**拒绝**（400）→ 把旧配置写回磁盘（否则下次重启起不来），且**不重启**；
  4. 上游不支持热生效（404/501）→ 回退到原来的重启路径。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.routers import settings as S  # noqa: E402


def _hot(**over) -> dict:
    base = {'ok': False, 'unsupported': False, 'rejected': False,
            'restart_required': [], 'message': ''}
    base.update(over)
    return base


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.json'
        self.original = {'api_key': 'k', 'schedule': {'checkin_hours': [9]}}
        self.path.write_text(json.dumps(self.original, ensure_ascii=False, indent=2),
                             encoding='utf-8')

        for patcher in (
            mock.patch.object(config, 'UPSTREAM_CONFIG', self.path),
            mock.patch.object(S.security, 'audit'),
            mock.patch.object(S, 'client_ip', lambda _r: '127.0.0.1'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        # 记录是否走了「重启容器」那条老路
        self.restarts: list[int] = []

        def _request_restart(*_a, **_kw) -> bool:
            self.restarts.append(1)
            return True

        for patcher in (
            mock.patch.object(S.reload, 'request_restart', _request_restart),
            mock.patch.object(S.reload, 'record_hot_apply'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, hot: dict, *, docker: bool = False) -> dict:
        with mock.patch.object(S.wb2api, 'apply_config_hot',
                               mock.AsyncMock(return_value=hot)), \
             mock.patch.object(S.updater, 'can_control_docker', lambda: docker):
            return asyncio.run(S.save_upstream(
                {'schedule': {'checkin_hours': [9, 21]}},
                request=mock.Mock(), user={'username': 'admin'}))

    def _on_disk(self) -> dict:
        return json.loads(self.path.read_text(encoding='utf-8'))

    # ① 热生效成功、无需重启
    def test_hot_apply_without_restart_required(self) -> None:
        out = self._run(_hot(ok=True))
        self.assertTrue(out['hot_applied'])
        self.assertFalse(out['reload_scheduled'])
        self.assertNotIn('reload_hint', out, '已热生效就不该再提示去宿主机重启')
        self.assertEqual(self.restarts, [], '热生效成功时绝不能再去重启容器')
        S.reload.record_hot_apply.assert_called_once()
        self.assertEqual(self._on_disk()['schedule']['checkin_hours'], [9, 21])

    # ② 热生效成功，但部分字段必须重启（docker 不可用 → 如实提示手动重启）
    def test_hot_apply_with_restart_required(self) -> None:
        out = self._run(_hot(ok=True, restart_required=['listen', 'auth_dir']))
        self.assertTrue(out['hot_applied'])
        self.assertIn('listen', out['reload_hint'])
        self.assertIn('auth_dir', out['reload_hint'])
        self.assertEqual(self.restarts, [], 'docker 不可用时不应尝试重启')

    # ②b 热生效成功 + 重启已排队 → **不能**再提示手动重启（否则自相矛盾）
    def test_hot_apply_with_scheduled_restart_has_no_hint(self) -> None:
        out = self._run(_hot(ok=True, restart_required=['listen']), docker=True)
        self.assertTrue(out['hot_applied'])
        self.assertTrue(out['reload_scheduled'], 'docker 可用时应排上重启')
        self.assertNotIn('reload_hint', out,
                         '重启已排队时再提示「请到宿主机手动重启」是自相矛盾的')
        self.assertEqual(self.restarts, [1])

    # ③ 上游拒绝：回滚磁盘，且不重启
    def test_rejected_rolls_back_and_does_not_restart(self) -> None:
        before = self.path.read_text(encoding='utf-8')
        out = self._run(_hot(rejected=True, message='cooldown.soft_rate: 非法时长'), docker=True)
        self.assertFalse(out['hot_applied'])
        self.assertFalse(out['reload_scheduled'])
        self.assertIn('上游拒绝了这份配置', out['reload_hint'])
        self.assertEqual(self.restarts, [],
                         '配置被拒时重启只会把旧配置再加载一遍，必须不重启')
        self.assertEqual(self.path.read_text(encoding='utf-8'), before,
                         '被拒绝的配置不能留在盘上（下次重启会起不来）')

    # ④ 上游不支持热生效 → 回退到重启路径
    def test_unsupported_falls_back_to_restart(self) -> None:
        out = self._run(_hot(unsupported=True, message='上游不支持配置热生效接口'),
                        docker=True)
        self.assertFalse(out['hot_applied'])
        self.assertTrue(out['reload_scheduled'])
        self.assertEqual(self.restarts, [1], '不支持热生效时应回退到重启容器')
        self.assertNotIn('reload_hint', out)

    # ④b 不支持热生效 + docker 也不可用 → 如实提示
    def test_unsupported_and_no_docker_hints_manual_step(self) -> None:
        out = self._run(_hot(unsupported=True), docker=False)
        self.assertFalse(out['reload_scheduled'])
        self.assertIn('docker compose restart', out['reload_hint'])


if __name__ == '__main__':
    unittest.main()


class DirectSentinelTest(unittest.TestCase):
    """`__direct__` 的语义与保留名约束。

    `http_client(proxy=None)` 的真实语义是「用全局出口代理」，不是直连 —— 所以
    resolve_account_proxy 必须返回空串。返回 None 会让「直连」账号在配了全局
    代理时仍然走代理，与用户意图完全相反。
    """

    def test_direct_returns_empty_string_not_none(self) -> None:
        from server import config
        with mock.patch.object(config, 'proxy_routes', lambda: {'resin': 'http://x:1'}), \
             mock.patch.object(config, 'proxy_route_enabled', lambda: {}):
            got = config.account_proxy({'proxy': config.DIRECT_ROUTE_NAME, 'uid': 'u', 'nickname': 'n'})
        self.assertEqual(got, '', '直连必须返回空串（None 会被 http_client 当成「用全局代理」）')

    def test_direct_name_cannot_be_used_as_route(self) -> None:
        from server.services import proxyroutes
        with self.assertRaises(proxyroutes.RouteError):
            proxyroutes.validate_name('__direct__')


class HotApplyStateTest(unittest.TestCase):
    """热生效不应计入 restart_count（那个计数器的语义是「重启了几次容器」）。"""

    def test_record_hot_apply_does_not_count_as_restart(self) -> None:
        from server.services import reload as R
        before = R.state()['restart_count']
        R.record_hot_apply('配置已热生效')
        after = R.state()
        self.assertEqual(after['restart_count'], before, '热生效不是重启，不该递增 restart_count')
        self.assertTrue(after['last_ok'])
