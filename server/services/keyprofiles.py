"""密钥 profile 的跨仓一致性：realm 一致 + 绑定上游允许集（迁移 Todo 20）。

## 三个判据，各自的失败形态

1. **profile ↔ realm 必须一致**（`realm_conflict`）。
   realm 选账号池，profile 选端点族（契约 §1）。`intl-cli` + `realm=cn` 不是
   「更细的限制」，而是自相矛盾：core 拼出 `www.codebuddy.ai` 的 URL，却只从
   cn 池选号 —— 每次调用都空池/502，而界面上完全看不出配错了。写入前拒掉。

2. **profile 必须 ⊆ 绑定上游 key 的 profiles**（`subset_conflict`）。
   core 侧 `api_keys[].profiles` 是允许集，manager 的 `api_keys.profile` 是单值。
   允许一个 core 那侧不允许的值，症状是「保存成功、调用被 400
   profile_not_allowed」。把这类不一致挡在保存时。

3. **读不到就跳过**（`fetch_allowed` 返回 `(None, warning)`）。
   上游不可达 / 该 key 不在上游配置里 / 未绑定 —— 都不代表配置非法，只是
   「此刻没法验证」。跳过并记 warning：宁可少校验一次，也不要把保存卡死。

## 为什么默认上游读文件、非默认走 panel API

默认上游的 config.json 就在本机（`config.UPSTREAM_CONFIG`），直接读是唯一真相，
不必打一趟 HTTP。非默认上游的配置文件在本机**不存在**（另一个实例 / 容器），
只能问它自己的 `/panel/api/config`（与 settings 的写入路径同源，见
`wb2api.save_upstream_config_remote`）。

## 为什么用同步桥 + 项目自己的 http_client

本模块被同步路由（`def create_key` / `def update_key`）调用，跑在线程池里，
而 `config.http_client` 是 async。这里照抄 `modelcatalog.fetch_ids_blocking`
的取舍：线程池线程没有运行中的循环，`asyncio.run` 自建一个临时循环是安全的；
**已经**有循环时就不自建（返回"读不到" → 跳过校验），绝不把请求卡死。

为什么不用 `httpx.get`：它默认 `trust_env=True`，会走环境变量里的代理 ——
本项目的 `config.http_client` 刻意忽略系统代理（内网请求不该被劫持）。
一个本地/内网的 `/panel/api/config` 被代理走，症状是「校验随机跳过」，
排查成本远高于多写这十行。

鉴权头复用 `wb2api._auth_headers`（与 settings 的写入路径同源），避免第三份
「Bearer 怎么拼」的实现。`_get_json` 是唯一的 IO 缝，测试直接替换它。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .. import config, upstreamsvc

logger = logging.getLogger('workbuddy.keys')

DEFAULT = None

# 契约 §1 的 4 个 profile。空串 = 不限（不在此集合里，单独处理）。
PROFILES = ('cn-cli', 'cn-work', 'intl-cli', 'intl-work')


def profile_realm(profile: object) -> str:
    """由 profile 推导 realm：`cn-*` → cn、`intl-*` → global；认不出返回 ''。

    与 core 的 `auth.ProfileRealm` 同一份判据（两仓各一份是因为跑在不同进程里，
    但取值必须逐字相同）。**不编造**：认不出就不返回 realm —— 猜错会让 core
    按错误的版本选号，比不带头更糟。
    """
    name = str(profile or '').strip().lower()
    if name.startswith('intl-'):
        return 'global'
    if name.startswith('cn-'):
        return 'cn'
    return ''


def normalize_profile(value: object) -> str:
    """归一化 profile：只接受契约 §1 的 4 个值；其余（含空）→ ''。"""
    name = str(value or '').strip().lower()
    return name if name in PROFILES else ''


def profile_for_domain(domain: object) -> str:
    """按 4-host 表推导 profile；共用 host 不猜（返回 ''）。

    与 core `profileForDomain` 同一张表：只列能唯一确定 profile 的 host。
    """
    d = str(domain or '').strip().lower()
    return {
        'copilot.tencent.com': 'cn-cli',
        'www.workbuddy.cn': 'cn-work',
        'www.codebuddy.ai': 'intl-cli',
        'www.workbuddy.ai': 'intl-work',
    }.get(d, '')


def resolve_profile(explicit: object, derived: object, raw_realm: object,
                    domain: object) -> str:
    """契约 §1 的 profile 选择链（manager 侧只读展示用）。

    explicit > persisted derived（调用方只在 profile_backfill_v1=true 时传
    derived）> stored realm > domain > cn-cli。

    **与 core 的唯一区别**：core 的 `ResolveProfile` 不校验 explicit 是否在
    4 个值内（那是写入侧 `_norm_profile` 的职责）；这里读的是磁盘上的存量文件，
    可能来自旧版本/手写，所以认不出的 explicit 不采纳，继续往下推导。
    """
    p = normalize_profile(explicit)
    if p:
        return p
    p = normalize_profile(derived)
    if p:
        return p
    realm = str(raw_realm or '').strip().lower()
    if realm == 'global':
        return 'intl-work'
    if realm == 'cn':
        return 'cn-cli'
    return profile_for_domain(domain) or 'cn-cli'


def realm_conflict(profile: object, realm: object) -> str | None:
    """返回冲突说明；None = 一致（含任一为空 = 不限的情况）。

    只校验**两者都非空**的组合：任一为空表示「不限」，不存在矛盾。
    """
    prof = str(profile or '').strip().lower()
    rlm = str(realm or '').strip().lower()
    if not prof or not rlm:
        return None
    want = profile_realm(prof)
    if not want:
        # 未知 profile 已在 keysvc._norm_profile 收敛成 ''，走不到这里；
        # 保守放行而不是编造冲突（这里不负责校验 profile 取值本身）。
        return None
    if want != rlm:
        return (f'profile「{prof}」属于 {want} 版本，与密钥的版本归属「{rlm}」冲突：'
                f'{prof} 的端点只在 {want} 账号池上，配成 {rlm} 会让每次调用都打空池')
    return None


def subset_conflict(profile: object, allowed: set[str] | None) -> str | None:
    """manager 的单值 profile 是否在 core key 的允许集内；None = 通过。

    `allowed is None` = 上游那把 key 不限（省略 profiles / 空数组 / 含 "*"），
    任何 profile 都通过。manager 侧空 profile = 不限，同样通过。
    """
    prof = str(profile or '').strip().lower()
    if not prof or allowed is None:
        return None
    if prof in allowed or '*' in allowed:
        return None
    listed = '、'.join(sorted(allowed)) if allowed else '（空）'
    return (f'profile「{prof}」不在绑定上游的密钥允许集内（上游允许：{listed}）。'
            '请先在 core 的 config.json 的 api_keys[].profiles 里放开它，'
            '或改选一个已允许的 profile')


def allowed_from_config(cfg: object, forward_key: object) -> set[str] | None:
    """从 core config 对象里取某把 key 的 profiles；None = 不限 / 没找到。

    **按 key 精确匹配**（不是按顺序取第一条）：同一个上游可能有多把分组密钥，
    每把的 profiles 不同；拿错那把等于校验了别人的白名单。
    """
    key = str(forward_key or '')
    if not key or not isinstance(cfg, dict):
        return None
    entries = cfg.get('api_keys')
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get('key') or '') != key:
            continue
        raw = entry.get('profiles')
        if not isinstance(raw, list):
            return None
        vals = {str(x).strip().lower() for x in raw if str(x).strip()}
        if not vals or '*' in vals:
            return None
        return vals
    return None


def _get_json(url: str, headers: dict) -> Any:
    """同步 GET + JSON 解析（唯一的 IO 缝，测试直接替换它）。

    超时压得很短（5s / connect 2s）：这是保存密钥路径上的一次**附加校验**，
    不该让一次慢上游把管理端的保存按钮转上 20 秒。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError('当前上下文已有事件循环，未同步读取上游配置')

    async def run() -> Any:
        async with config.http_client(5, connect=2) as client:
            resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    return asyncio.run(run())


def fetch_allowed(upstream: dict | None) -> tuple[set[str] | None, str | None]:
    """读绑定上游的允许集，返回 `(allowed, warning)`。

    · `allowed is None` = 不限 / 读不到；`warning` 非空时说明为什么读不到。
    · 默认上游（None 或 is_default）读本机 `config.UPSTREAM_CONFIG`。
    · 非默认上游打它自己的 `/panel/api/config`，鉴权用 `forward_api_key`
      （同址且未单独填 key 的分组沿用默认那把，见 upstreamsvc）。
    """
    if upstream is None or upstream.get('is_default'):
        return _allowed_from_local_file()

    base = str(upstream.get('base_url') or '').strip().rstrip('/')
    if not base:
        return None, '绑定上游未配置 base_url，跳过 profile 允许集校验'

    from . import wb2api  # 延迟导入：wb2api 依赖面很大，模块顶层导入会成环

    key = upstreamsvc.forward_api_key(upstream)
    headers = wb2api._auth_headers(key)
    url = f'{base}/panel/api/config'
    try:
        payload = _get_json(url, headers)
    except Exception as exc:  # noqa: BLE001
        # 不可达不该阻断保存：只是「此刻没法验证」。warning 进日志供排查。
        msg = f'无法读取上游 {base} 的 api_keys（{exc}），跳过 profile 允许集校验'
        logger.warning(msg)
        return None, msg

    cfg = payload.get('config') if isinstance(payload, dict) else None
    if not isinstance(cfg, dict):
        msg = f'上游 {base} 的 /panel/api/config 未返回 config 对象，跳过 profile 允许集校验'
        logger.warning(msg)
        return None, msg
    allowed = allowed_from_config(cfg, key)
    if allowed is None:
        msg = f'上游 {base} 的 api_keys 里没有与当前密钥匹配的条目（或该条目不限 profile），跳过允许集校验'
        logger.warning(msg)
        return None, msg
    return allowed, None


def _allowed_from_local_file() -> tuple[set[str] | None, str | None]:
    try:
        cfg = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8'))
    except Exception as exc:  # noqa: BLE001
        msg = f'读取上游 config.json 失败（{exc}），跳过 profile 允许集校验'
        logger.warning(msg)
        return None, msg
    if not isinstance(cfg, dict):
        msg = '上游 config.json 不是 JSON 对象，跳过 profile 允许集校验'
        logger.warning(msg)
        return None, msg
    # 用 forward_api_key 精确匹配 api_keys[] 的条目（契约 §1）。主密钥通常**不在**
    # 那张表里 —— 那属于「没有匹配条目」，按契约跳过并记 warning（不静默当成不限：
    # 静默会让「上游其实限制得更严」这种情况查不出来）。
    key = config.upstream_api_key()
    # 主密钥（顶层 api_key）按定义**不限 profile**（契约 §1：空/省略 = 不限，
    # 主密钥等价于「不在这张表里」），所以这里既不校验也不记 warning ——
    # 每保存一把密钥都刷一条「找不到条目」只会训练运维忽略这个日志。
    if key and key == str(cfg.get('api_key') or ''):
        return None, None
    allowed = allowed_from_config(cfg, key)
    if allowed is None:
        msg = ('上游 config.json 的 api_keys 里没有与当前密钥匹配的条目'
               '（或该条目不限 profile），跳过允许集校验')
        logger.warning(msg)
        return None, msg
    return allowed, None
