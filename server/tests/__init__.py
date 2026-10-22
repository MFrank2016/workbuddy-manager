"""测试包入口 —— **数据目录保险**。

## 为什么需要它（真实事故）

一次 `docker run -v <repo>:/app ... python -m unittest` 把测试跑在了镜像默认的
`WB_DATA_DIR=/app/data` 上 —— 而那个路径正好是挂载进来的**生产数据目录**。
测试把 `users.json` 覆盖成了夹具（`"secret":"S"`、`users: []`），属主还是 root，
容器里的进程（uid 10001）随即读不了它；`security.load_users()` 按设计拒绝自动
重建以保护现场，于是**所有需要登录态的接口全部 500**（页面能打开、数据全是错的）。

## 它做什么

在**导入测试包时**检查 `WB_DATA_DIR` / `WB_AUTH_DIR`：只要看起来是真实部署的
（存在 `manager.db` / `users.json`，或授权目录里有 `workbuddy-*.json`），就直接
拒绝运行并给出正确用法 —— 让「跑错目录」在**第一条测试执行之前**就失败。

确需在真实数据上跑（例如手工复现某个问题），显式设 `WB_TEST_ALLOW_REAL_DATA=1`。
"""
from __future__ import annotations

import os
from pathlib import Path

# 真实数据目录的标志物：有其中任何一个，就说明这是部署现场而不是测试临时目录。
_DATA_MARKERS = ('manager.db', 'users.json')
# 上游授权目录的标志物：workbuddy-<uid>.json（真实账号凭据）。
_AUTH_GLOB = 'workbuddy-*.json'


def _looks_like_real_data(path: Path) -> bool:
    try:
        if any((path / name).exists() for name in _DATA_MARKERS):
            return True
        return any(path.glob(_AUTH_GLOB))
    except OSError:
        # 目录不存在 / 没权限读：当成「不是真实数据」，交给测试自己报错。
        return False


def _guard() -> None:
    if os.environ.get('WB_TEST_ALLOW_REAL_DATA') == '1':
        return

    from server import config

    checks = (
        ('数据目录', 'WB_DATA_DIR', Path(config.DATA_DIR)),
        ('授权目录', 'WB_AUTH_DIR', Path(config.AUTH_DIR)),
    )
    for label, env, path in checks:
        if not _looks_like_real_data(path):
            continue
        raise RuntimeError(
            f'拒绝在真实{label}上运行测试：{path}\n'
            f'  检测到 {" / ".join(_DATA_MARKERS)} 或 {_AUTH_GLOB} —— 这看起来是部署现场。\n'
            f'  请把 {env} 指到临时目录后重跑，例如：\n'
            f'    {env}=/tmp/wb-test python -m unittest discover -s server/tests -t .\n'
            f'  确需在真实数据上运行：设 WB_TEST_ALLOW_REAL_DATA=1（会直接改写该目录，后果自负）。'
        )


_guard()
