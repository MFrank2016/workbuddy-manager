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

唯一的例外是**仓库自带的默认数据目录**（`<repo>/data`）：它是开发机上常见的
部署现场，但也是 `python -m unittest discover` 的默认落点。此时不报错，而是把
整个测试进程改指到一个临时目录（并在退出时删除），既让规范命令能跑，又保证
真实数据一个字节都不会被写。**显式**把 `WB_DATA_DIR` / `WB_AUTH_DIR` 指向真实
目录仍然拒绝运行 —— 那是用户明确要求，不能替他做主。

确需在真实数据上跑（例如手工复现某个问题），显式设 `WB_TEST_ALLOW_REAL_DATA=1`。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
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


def _redirect_default_data_dir(config) -> bool:
    """仓库默认 `data/` 是真实部署时，把测试改指到临时目录。

    只在**没有显式设置** `WB_DATA_DIR` / `WB_AUTH_DIR` 时生效：默认值指向
    部署现场属于「跑测试时忘了隔离」，替用户隔离是安全的；显式指定则尊重
    用户意图，交给 `_guard` 拒绝。
    """
    if os.environ.get('WB_TEST_ALLOW_REAL_DATA') == '1':
        return False
    if os.environ.get('WB_DATA_DIR', '').strip() or os.environ.get('WB_AUTH_DIR', '').strip():
        return False

    default_data = (Path(config.ROOT) / 'data').resolve()
    data_path = Path(config.DATA_DIR).resolve()
    auth_path = Path(config.AUTH_DIR).resolve()
    default_auth = default_data / 'auths'
    if data_path != default_data and auth_path != default_auth:
        return False
    real_paths = [p for p in (data_path, auth_path) if _looks_like_real_data(p)]
    if not real_paths:
        return False
    # 只有「真实路径全部是仓库默认落点」才隔离；显式指到别处的真实目录仍交给
    # `_guard` 拒绝，避免把生产凭据悄悄换掉。
    if any(p not in (default_data, default_auth) for p in real_paths):
        return False

    tmp = Path(tempfile.mkdtemp(prefix='wb-test-'))
    auth = tmp / 'auths'
    auth.mkdir()
    config.DATA_DIR = tmp
    config.DB_PATH = tmp / 'manager.db'
    config.USERS_FILE = tmp / 'users.json'
    config.AUTH_DIR = auth
    os.environ['WB_DATA_DIR'] = str(tmp)
    os.environ['WB_AUTH_DIR'] = str(auth)
    os.environ['WB_DB'] = str(config.DB_PATH)
    os.environ['WB_USERS_FILE'] = str(config.USERS_FILE)
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    return True

def _guard() -> None:
    if os.environ.get('WB_TEST_ALLOW_REAL_DATA') == '1':
        return

    from server import config

    # 默认落点恰好是仓库里的真实部署：隔离到临时目录，而不是让规范命令失效。
    if _redirect_default_data_dir(config):
        return

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
