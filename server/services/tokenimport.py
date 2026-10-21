"""批量 refresh token 导入：解析、强制刷新、有界并发、逐条隔离。

## 为什么单独一个模块

`/api/accounts/upload` 信任客户端给的 accessToken（离线拷贝即可用）；这个入口
**只信 refreshToken**——每条都当场找腾讯换一次，落盘的一定是刚刚签发的凭据。
由此带来的三件事都集中在这里，而不是散在路由里：

  1. **条目形态解析**：完整 auth JSON 的 `{account, auth}` 嵌套形与扁平条目都认，
     嵌套同名键优先；realm 优先级 `body.realm > auth.realm > domain 推导`；
  2. **有界执行**：上限 200 条、并发 2、逐条 30s、整体 120s。腾讯的刷新接口对
     并发很敏感（风控），所以上限是刻意压低的；
  3. **逐条隔离**：一条缺 uid 或刷新失败不能拖垮整批，也不能让「失败」看起来
     像「成功」。结果按 `added|updated|skipped|rejected|failed` 分类返回。

## 安全口径

- 客户端提供的 `accessToken` / `expires_at` **一律忽略**：它们可能已过期，也可能
  根本不属于这个 refreshToken；
- 任何结果、审计、日志里都**不得**出现 accessToken / refreshToken 原文。失败原因
  只用固定短码（`refresh_failed` / `write_failed` / …），不把异常文本透给调用方。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path

from . import tencent
from .filelock import file_lock
from .realm import resolve_realm

# 上限与预算。**刻意做成模块级常量**：它们是协议的一部分（前端按这个上限做
# 预校验），也便于测试用 patch 缩短时间而不必等真实的 120 秒。
MAX_ENTRIES = 200
CONCURRENCY = 2
ENTRY_TIMEOUT = 30.0
DEADLINE = 120.0

# uid 会拼进文件名，所以与 `tencent.write_auth_file` 用同一套字符集白名单。
# 这里先判一次，是为了让「uid 形态非法」落进 rejected 而不是让整批 500。
_UID_RE = re.compile(r'[0-9A-Za-z_-]{1,80}')

_VALID_REALMS = ('cn', 'global')

# 单进程内同时只允许一个导入：每个条目都要打腾讯的刷新接口，两个导入并发会把
# 请求数翻倍（风控）并让进度无法解释。跨进程不共享这个标记——面板本来就按
# 单实例部署，真要多实例各导各的也不会互相破坏（写盘走 file_lock + 原子替换）。
_guard = threading.Lock()
_running = False


def try_begin() -> bool:
    """抢占导入权；已有导入在跑时返回 False（路由据此回 409）。"""
    global _running
    with _guard:
        if _running:
            return False
        _running = True
        return True


def end() -> None:
    """释放导入权。**必须放在 finally 里**，否则一次异常会让面板永久 409。"""
    global _running
    with _guard:
        _running = False


def _text(value: object) -> str:
    return str(value).strip() if value not in (None, '') else ''


def _first(*values: object) -> str:
    """取第一个非空值（嵌套优先的调用方按优先级传参）。"""
    for value in values:
        text = _text(value)
        if text:
            return text
    return ''


def parse_entry(raw: object) -> tuple[dict, str]:
    """把一条导入条目归一化为 refresh/write 所需的字段。

    返回 `(fields, reason)`；`reason` 非空表示这条要记 rejected。
    **不抛异常**：一条脏数据不该让整批中断。

    嵌套形 `{account, auth}` 里同名字段优先于扁平字段（用户从面板导出的完整
    auth JSON 就是这个形态；扁平字段可能只是旧版导出的残留）。
    """
    if not isinstance(raw, dict):
        return {}, 'invalid_entry'
    account = raw.get('account') if isinstance(raw.get('account'), dict) else {}
    auth = raw.get('auth') if isinstance(raw.get('auth'), dict) else {}

    uid = _first(account.get('uid'), account.get('user_id'), raw.get('uid'),
                 raw.get('user_id'))
    if not uid:
        return {}, 'missing_uid'
    if not _UID_RE.fullmatch(uid):
        return {}, 'invalid_uid'
    refresh = _first(auth.get('refreshToken'), auth.get('refresh_token'),
                     raw.get('refreshToken'), raw.get('refresh_token'))
    if not refresh:
        return {}, 'missing_refresh_token'

    return {
        'uid': uid,
        'refresh_token': refresh,
        # 客户端 accessToken 只作为出站头的形态占位，**绝不**落盘：
        # 下面 refresh 时会显式传空串，确保没人拿它当结果。
        'domain': _first(auth.get('domain'), raw.get('domain')),
        'realm': _first(auth.get('realm'), raw.get('realm')),
        'nickname': _first(account.get('nickname'), account.get('name'),
                           raw.get('nickname')),
        'enterprise_id': _first(account.get('enterpriseId'),
                                account.get('enterprise_id'),
                                raw.get('enterpriseId'), raw.get('enterprise_id')),
        'profile': _first(account.get('profile'), raw.get('profile')),
        'device_token': _first(raw.get('device_token')),
    }, ''


def resolve_entry_realm(body_realm: str, entry: dict) -> str:
    """realm 优先级：`body.realm > auth.realm > domain 推导`。

    落盘用的是 `resolve_realm`（不含逃生门），与登录/导入同口径：开了
    `global.enabled=false` 的逃生门也不该把国际版账号永久写成 cn。
    """
    if body_realm:
        return body_realm
    return resolve_realm(entry.get('realm'), entry.get('domain'))


def valid_realm(value: object) -> str:
    """校验 body.realm：空 / cn / global；其余由路由转 400。"""
    text = _text(value).lower()
    if text and text not in _VALID_REALMS:
        raise ValueError('realm 只能是 cn 或 global')
    return text


def ensure_writable(auth_dir: Path) -> None:
    """确保目录存在且可写；不满足时抛 OSError（路由转 400）。

    目录不存在时按 0750 新建（账号目录不对其他用户开放）：`mkdir(mode=)` 受
    umask 影响，所以显式 chmod，与账号文件权限的显式 chmod 同一理由。
    """
    if auth_dir.is_dir():
        if not os.access(auth_dir, os.W_OK):
            raise PermissionError(f'目录不可写：{auth_dir}')
        return
    auth_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    try:
        os.chmod(auth_dir, 0o750)
    except OSError:
        # Windows / 只读挂载上没有权限位语义；能写就行，不为权限位打断导入。
        pass


def target_path(auth_dir: Path, uid: str) -> Path:
    """与 `write_auth_file` 相同的目标选择：只存在 `.disabled` 时写它。"""
    target = auth_dir / f'workbuddy-{uid}.json'
    disabled = auth_dir / f'{target.name}.disabled'
    if not target.exists() and disabled.exists():
        return disabled
    return target


def _result(uid: str, status: str, reason: str = '', message: str = '') -> dict:
    return {'uid': uid, 'status': status, 'reason': reason, 'message': message}


def _rejected(entry: dict, reason: str, message: str = '') -> dict:
    return _result(str(entry.get('uid') or ''), 'rejected', reason, message)


async def import_entries(entries: list[object], *, realm: str, auth_dir: Path,
                         overwrite: bool) -> list[dict]:
    """逐条强制刷新并落盘，返回**与输入同序**的结果列表。

    预算的两层含义：
      · `ENTRY_TIMEOUT` —— 单条（网络那一步）最多等多久；
      · `DEADLINE` —— 整批最多跑多久；到点后**还没开始**的条目直接记
        `failed/timeout`，不再发起刷新（继续打腾讯只会把风控拖进来）。
    """
    started = time.monotonic()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(
        _one(raw, realm=realm, auth_dir=auth_dir, overwrite=overwrite,
             started=started, semaphore=semaphore)
        for raw in entries
    ))
    return list(results)


async def _one(raw: object, *, realm: str, auth_dir: Path, overwrite: bool,
               started: float, semaphore: asyncio.Semaphore) -> dict:
    entry, reason = parse_entry(raw)
    if reason:
        return _rejected(entry, reason, _reason_text(reason))

    uid = entry['uid']
    resolved = resolve_entry_realm(realm, entry)
    if target_path(auth_dir, uid).exists() and not overwrite:
        return _result(uid, 'skipped', 'exists', '账号已存在，未覆盖')
    if time.monotonic() - started >= DEADLINE:
        return _result(uid, 'failed', 'timeout', '超出整批时限，未开始')

    async with semaphore:
        # 排队期间可能已经到点：真正的「未开始」判定在这里。
        if time.monotonic() - started >= DEADLINE:
            return _result(uid, 'failed', 'timeout', '超出整批时限，未开始')
        try:
            ok, _message, fields = await asyncio.wait_for(
                tencent.refresh_token({
                    # **故意传空串**：客户端的 accessToken 不可信，也不该出现在
                    # 出站 Authorization 里——刷新只认 refreshToken。
                    'access_token': '',
                    'refresh_token': entry['refresh_token'],
                    'uid': uid,
                    'enterprise_id': entry['enterprise_id'],
                    'domain': entry['domain'],
                    'realm': resolved,
                    'device_token': entry['device_token'],
                }),
                ENTRY_TIMEOUT,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return _result(uid, 'failed', 'timeout', '刷新超时')
        except Exception:  # noqa: BLE001
            # 异常文本可能带 URL / 凭据，绝不外传。
            return _result(uid, 'failed', 'refresh_failed', '刷新异常')

    if not ok:
        return _result(uid, 'failed', 'refresh_failed', '刷新失败，需重新登录')

    return _write(entry, fields, uid=uid, resolved=resolved, auth_dir=auth_dir,
                  overwrite=overwrite)


def _expires_at_for_write(fields: dict, auth_dir: Path, uid: str) -> int:
    """决定本次落盘的 `expiresAt`。

    优先级：**刷新响应** → **旧文件里的已知正数**（覆盖导入时保留）→ **0**
    （未知）。0 绝不能覆盖已知正数：那会让 `renew._needs_renew` 从此返回
    False，自动续期永久跳过这个账号。旧值读不出来一律当「未知」，不抛。
    """
    fresh = fields.get('expires_at')
    if isinstance(fresh, (int, float)) and not isinstance(fresh, bool):
        return int(fresh)
    try:
        raw = json.loads(target_path(auth_dir, uid).read_text(encoding='utf-8'))
        old = (raw.get('auth') or {}).get('expiresAt')
    except (OSError, ValueError, AttributeError):
        return 0
    if isinstance(old, (int, float)) and not isinstance(old, bool) and old > 0:
        return int(old)
    return 0


def _write(entry: dict, fields: dict, *, uid: str, resolved: str,
           auth_dir: Path, overwrite: bool) -> dict:
    """把刷新结果落盘。

    `expires_at` 取值：**刷新响应优先**；响应没给时保留旧文件里的已知到期
    时间，不能把正数盖成 0 —— 否则自动续期会永久跳过这个账号。
    """
    account = {
        'uid': uid,
        'access_token': fields['access_token'],
        'refresh_token': str(fields.get('refresh_token') or entry['refresh_token']),
        'expires_at': 0,          # 下面按「响应 → 旧文件 → 未知(0)」填入
        'domain': str(fields.get('domain') or entry['domain'] or ''),
        'realm': resolved,
        'nickname': entry['nickname'],
        'enterprise_id': entry['enterprise_id'],
        'profile': entry['profile'],
        'device_token': entry['device_token'],
    }
    try:
        # 读旧值 + 落盘在同一把文件锁里（RLock，可重入）：否则并发的续期/重登
        # 刚写好的到期时间可能被这里的旧快照盖回去（见 filelock.py）。
        with file_lock(target_path(auth_dir, uid)):
            account['expires_at'] = _expires_at_for_write(fields, auth_dir, uid)
            _name, existed = tencent.write_auth_file(
                account, auth_dir, allow_overwrite=overwrite)
    except tencent.AccountExistsError:
        return _result(uid, 'skipped', 'exists', '账号已存在，未覆盖')
    except ValueError:
        return _result(uid, 'rejected', 'invalid_uid', '账号 uid 形态异常')
    except OSError:
        return _result(uid, 'failed', 'write_failed', '账号写入失败')
    except Exception:  # noqa: BLE001
        return _result(uid, 'failed', 'write_failed', '账号写入失败')
    return _result(uid, 'updated' if existed else 'added')


_REASON_TEXT = {
    'invalid_entry': '条目必须是对象',
    'missing_uid': '缺少 uid',
    'invalid_uid': '账号 uid 形态异常',
    'missing_refresh_token': '缺少 refreshToken',
}


def _reason_text(reason: str) -> str:
    return _REASON_TEXT.get(reason, '条目无效')


def summarize(results: list[dict]) -> dict[str, int]:
    """按状态计数（审计只记这些数字，绝不记 token）。"""
    counts = {'added': 0, 'updated': 0, 'skipped': 0, 'rejected': 0, 'failed': 0}
    for item in results:
        status = str(item.get('status') or '')
        if status in counts:
            counts[status] += 1
    return counts
