"""上游配置、模型映射与管理端用户。"""
from __future__ import annotations

import json
import os
import stat
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import config, db, retention, security
from ..services.filelock import file_lock
from ..iputil import client_ip
from ..services import modelmap, modelrouteadmin, reload, updater, wb2api

router = APIRouter(prefix='/api', tags=['settings'])


# ── 配置文件读写的三个小助手（热生效 / 回滚用）─────────────
def _read_config_raw() -> str | None:
    """保存前的原文；读不到返回 None（此时回滚视为失败）。"""
    try:
        return config.UPSTREAM_CONFIG.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        # 非 UTF-8 的 config.json：read_text 抛 UnicodeDecodeError（ValueError 子类，
        # 不是 OSError）。不接住的话它会从 save_upstream 的 try 之前逃出去变成 500。
        return None


def _read_written_config() -> dict | None:
    """刚写入磁盘的配置对象（与线路 CRUD 共用同一份读取逻辑）。"""
    return wb2api.load_config_file()


def _write_config_raw(raw: str | None) -> bool:
    """原子写回原文。raw 为 None（保存前就没有配置文件）时返回 False。

    两个细节都必须照顾到：

    · **持同一把文件锁**：`save_upstream_config` 写盘时持 `file_lock(path)`。回滚若
      不持锁，两个管理员并发时会出现「A 读旧 → B 写新 → A 热生效 → A 回滚把 B 的
      新配置盖掉」，而 B 那边已经报过成功了。
    · **保留 mode/owner**：`write_text` + `replace` 建的新文件按 umask（通常 0644）
      与当前进程 uid 落地，会把上游文件的权限/属主悄悄改掉。
    """
    if raw is None:
        return False
    path = config.UPSTREAM_CONFIG
    try:
        with file_lock(path):
            st = path.stat()
            tmp = path.with_name(path.name + '.rollback')
            tmp.write_text(raw, encoding='utf-8')
            try:
                os.chmod(tmp, stat.S_IMODE(st.st_mode))
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                # 权限/属主恢复失败不影响内容正确性（非 root 改属主本来就可能失败）。
                pass
            os.replace(tmp, path)
        return True
    except OSError:
        return False


def _lookup_path(data: dict, path: str):
    """按 "a.b.c" 取值；中间不是对象时返回 None。"""
    cur: object = data
    for key in path.split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _changed_restart_fields(previous_raw: str | None, current: dict | None,
                            paths: list[str]) -> list[str]:
    """本次保存里「确实变了、且必须重启才生效」的字段。

    为什么在面板侧算：管理端**先写盘、再让上游热生效**，上游读到的已经是新配置，
    按文件差异算必然为空 —— 于是「改了 listen」永远不会提示重启。面板手里有保存前
    的快照，只有它知道变了什么。

    旧配置读不到（文件损坏/不存在）时保守返回全量清单：宁可多提示一次重启，也不要
    让「改了启动期字段却没生效」被静默吞掉。
    """
    if not paths or current is None:
        return []
    if previous_raw is None:
        return list(paths)
    try:
        previous = json.loads(previous_raw)
    except ValueError:
        return list(paths)
    if not isinstance(previous, dict):
        return list(paths)
    return [p for p in paths if _lookup_path(previous, p) != _lookup_path(current, p)]


def _restart_hint(fields: list[str]) -> str:
    """「必须重启上游」的提示；fields 非空时列出具体字段名。"""
    lead = '配置已写入'
    if fields:
        lead += '；以下字段需重启上游容器才会生效：' + '、'.join(fields)
    return lead + '。请在宿主机执行 docker compose restart wb2api（上游目录下）使其生效。'


# ── 上游配置 ─────────────────────────────────────────────
@router.get('/settings/upstream')
def get_upstream(user: dict = Depends(security.current_user)) -> dict:
    return wb2api.load_upstream_config()


@router.post('/settings/upstream')
async def save_upstream(body: dict, request: Request,
                        user: dict = Depends(security.require_session_admin)) -> dict:
    # 必须是 async：同步路由会被 FastAPI 放进线程池执行，那里没有事件循环，
    # 无法调度后台重载任务（request_restart 将拿不到 running loop）。
    # 保存前先留一份原文：上游若拒绝新配置，用它回滚（否则盘上会留下非法配置，
    # 下一次重启直接起不来）。
    previous_raw = _read_config_raw()
    try:
        result = wb2api.save_upstream_config(body)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 改上游配置会影响全部账号行为，且可能写入 device_token/base 等敏感项，必须留痕。
    # 顶层标量（如 reasoning_as_content）不属于任何段，单独列出——否则日志会显示
    # 「段=」，看起来像什么都没改。
    sections = ','.join(sorted(k for k in body if isinstance(body.get(k), dict)))
    top_keys = ','.join(sorted(
        f'{k}={body[k]}' for k in body
        if not isinstance(body.get(k), dict) and k != 'listen'
    ))
    security.audit(user, 'update_upstream', '',
                   f'段={sections}' + (f'；顶层={top_keys}' if top_keys else '')
                   + f'；来源 {client_ip(request)}')
    # 上游只在启动时读 config.json，保存后让它生效。分三级：
    #   ① 热生效（上游内嵌面板 /panel/api/config）—— 绝大多数字段无需重启；
    #   ② 上游拒绝（400）—— 回滚磁盘上的配置，**绝不**重启（重启只会重载旧配置，
    #      甚至因为盘上已是非法配置而起不来）；
    #   ③ 上游不支持热生效（旧版 / 未内嵌面板）或调用失败 —— 回退到重启容器。
    written = _read_written_config()
    hot = await wb2api.apply_config_hot(written) if written else {
        'ok': False, 'unsupported': False, 'rejected': False,
        'restart_required': [], 'message': '读取已写入的配置失败',
    }
    result['hot_applied'] = bool(hot['ok'])

    if hot['ok']:
        # 优先用面板自己的前后对比（上游读到的已是新文件，它的差异恒为空）。
        paths = hot.get('restart_paths') or []
        required = (_changed_restart_fields(previous_raw, written, paths)
                    if paths else hot['restart_required'])
        reload.record_hot_apply(
            '配置已热生效' if not required else '配置已热生效（部分字段需重启）')
        if not required:
            result['reload_scheduled'] = False
            return result
        # 有字段必须重启才生效：能控制 docker 就顺手重启，否则如实提示。
        #
        # **重启已排队时不要设 reload_hint**：前端优先看 reload_hint 并弹「请到宿主机
        # 手动重启」，而那一步面板已经替你做了 —— 两条信息互相矛盾，用户会去做多余的
        # 操作。排上重启时只置 reload_scheduled，让界面显示「正在应用」。
        if updater.can_control_docker() and reload.request_restart():
            result['reload_scheduled'] = True
        else:
            result['reload_scheduled'] = False
            result['reload_hint'] = _restart_hint(required)
        return result

    if hot['rejected']:
        # 上游认为这份配置非法：把旧配置放回磁盘，避免下次重启起不来。
        restored = _write_config_raw(previous_raw)
        result['reload_scheduled'] = False
        result['reload_hint'] = (
            f'上游拒绝了这份配置（{hot["message"]}），已恢复原配置，未重启容器。'
            if restored else
            f'上游拒绝了这份配置（{hot["message"]}）；回滚失败，请手动检查 config.json。'
        )
        return result

    # 不支持 / 调用失败：回到老路（重启容器）
    result['hot_error'] = hot['message']
    if updater.can_control_docker() and reload.request_restart():
        result['reload_scheduled'] = True
    else:
        result['reload_scheduled'] = False
        result['reload_hint'] = _restart_hint([])
    return result


@router.get('/upstream/reload-state')
def reload_state(user: dict = Depends(security.current_user)) -> dict:
    """上游重载状态，供前端展示「正在应用配置」。"""
    return reload.state()


class UpstashTestIn(BaseModel):
    url: str = ''
    # 留空则使用配置文件中已保存的 token
    token: str | None = None


@router.post('/settings/upstash/test')
async def test_upstash(body: UpstashTestIn,
                       user: dict = Depends(security.require_session_admin)) -> dict:
    """探测 Upstash 是否可用（走其 REST 接口 PING）。"""
    ok, message = await wb2api.test_upstash(body.url, body.token)
    return {'ok': ok, 'message': message}


@router.post('/settings/upstash/reload')
async def reload_upstream(user: dict = Depends(security.require_session_admin)) -> dict:
    """立即重启上游容器（等待结果）。一般无需手动调用——保存配置会自动重载。

    与会话绑定：它和 `/api/restart` 是**同一个动作**（都走 `reload.restart_now()`），
    后者在引入 API Token 时被划为「仅会话」。同一个动作不能因为走的是哪个路由就
    权限不同——否则 token 泄露者能从这里把上游重启掉，那条边界就形同虚设。
    """
    ok, message = await reload.restart_now()
    return {'ok': ok, 'message': message}


# ── 模型别名映射 ─────────────────────────────────────────
@router.get('/settings/model-map')
def get_model_map(user: dict = Depends(security.current_user)) -> dict:
    return db.get_setting('model_map', {}) or {}


@router.post('/settings/model-map')
def save_model_map(body: dict[str, str], user: dict = Depends(security.require_admin)) -> dict:
    """保存映射表。键既可以是真实别名，也可以是兜底的 `*`（issue #173）。

    校验集中在 `services/modelmap.validate`：这里只拒「落库后一定会坏」的那一类
    —— 目前只有「目标写成 `*`」（查表会把 `*` 当模型名发给上游，而界面上显示着
    "已设置兜底"，用户从报错里反推不出原因）。

    ValueError 要**转成 400**：服务端校验按 4xx 送达，浏览器里才能把 detail 那句
    原因显示出来；漏了这一步它就是 500，用户只看到「服务器错误」而拿不到该改什么
    （浏览器验收里真踩到过：直接调接口写 `{"*": "*"}` 得到的是 500 空 body）。
    """
    try:
        clean = modelmap.validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.set_setting('model_map', clean)
    return clean

# ── 日志保留策略（迁移 C2：`log_retention_overrides`）───────
@router.get('/settings/log-retention')
def get_log_retention(user: dict = Depends(security.current_user)) -> dict:
    """读当前保留策略：稀疏 override + 完整默认集 + 生效值 + 校验边界。

    **任何已登录用户都能读**（只读角色也要能看见“日志会留多久”）。写入口
    在下面，只对会话管理员开放。
    """
    return retention.view(db.get_setting(retention.SETTING_KEY, {}))


@router.post('/settings/log-retention')
def save_log_retention(body: dict, request: Request,
                       user: dict = Depends(security.require_session_admin)) -> dict:
    """保存保留策略（**仅会话管理员**，API Token 一律 403）。

    为什么只对会话开放：改保留期等于能**提前抹掉**请求日志与审计痕迹——
    与「清日志」同级，泄露的管理令牌不该能缩短保留期。

    三件事依次做（顺序有意义）：
      1. 校验并合并成**稀疏**对象（只存用户改过的键；非法值 400 且不改库）；
      2. 落库；
      3. 立即按新上限精确裁剪一次 `ip_access_logs` —— 否则用户把上限从 2 万
         改成 100 后，要等 500 次写入才看到效果，界面上会一直显示旧行数。
    审计只记「改了哪几项 → 新值」，不含任何凭据。
    """
    current = db.get_setting(retention.SETTING_KEY, {})
    current = current if isinstance(current, dict) else {}
    try:
        merged = retention.merge(current, body)
    except retention.InvalidRetention as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.set_setting(retention.SETTING_KEY, merged)
    db.prune_ip_access_logs(exact=True)
    changed = ','.join(f'{k}={v}' for k, v in sorted(merged.items()))
    security.audit(user, 'update_log_retention', '',
                   f'保留项：{changed or "（全部恢复默认）"}；来源 {client_ip(request)}')
    return retention.view(merged)

# ── 模型路由（迁移 Todo 6，契约 §2）──────────────────────
#
# 七个端点分两类权限：读（列表 / 总开关）任何登录用户都能看 —— 只读角色也要
# 知道「下游那个模型名实际打到哪儿」；写一律 `require_session_admin` —— 改路由
# 等于改「请求去哪」，与改上游分组同级，泄露的 admin 令牌不该能重写它。
#
# 错误码按「用户下一步该做什么」分类：
#   · 400 非法 / 分组不存在或已停用 → 改请求就能成功；
#   · 404 路由 id 不存在 → 换一条（或刷新列表）；
#   · 409 同一 (public_id, realm, group) 已存在 → 去改那条已有的行。
# 审计只记「哪一行、哪些字段」，不含请求体原文（这里没有凭据，但口径统一）。
@router.get('/settings/model-routes')
def get_model_routes(user: dict = Depends(security.current_user)) -> dict:
    return {'items': modelrouteadmin.list_routes(),
            'enabled': modelrouteadmin.switch_enabled()}


@router.post('/settings/model-routes')
def create_model_route(body: dict, request: Request,
                       user: dict = Depends(security.require_session_admin)) -> dict:
    try:
        created = modelrouteadmin.create_route(body)
    except modelrouteadmin.DuplicateRoute as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    security.audit(user, 'create_model_route', created['public_id'],
                   f"id={created['id']}；目标={created['target_model']}；"
                   f"realm={created['realm']!r}；分组={created['upstream_group_id']}；"
                   f'来源 {client_ip(request)}')
    return created


@router.patch('/settings/model-routes/{route_id}')
def update_model_route(route_id: int, body: dict, request: Request,
                       user: dict = Depends(security.require_session_admin)) -> dict:
    try:
        updated = modelrouteadmin.update_route(route_id, body)
    except modelrouteadmin.DuplicateRoute as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail='路由不存在')
    security.audit(user, 'update_model_route', updated['public_id'],
                   f"id={updated['id']}；字段={','.join(sorted(body)) or '无'}；"
                   f'来源 {client_ip(request)}')
    return updated


@router.delete('/settings/model-routes/{route_id}')
def delete_model_route(route_id: int, request: Request,
                       user: dict = Depends(security.require_session_admin)) -> dict:
    deleted = modelrouteadmin.delete_route(route_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail='路由不存在')
    security.audit(user, 'delete_model_route', deleted['public_id'],
                   f"id={deleted['id']}；目标={deleted['target_model']}；"
                   f'来源 {client_ip(request)}')
    return {'ok': True, 'id': deleted['id']}


@router.post('/settings/model-routes/{route_id}/enabled')
def set_model_route_enabled(route_id: int, body: dict, request: Request,
                            user: dict = Depends(security.require_session_admin)) -> dict:
    enabled = body.get('enabled')
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail='enabled 只能是 true 或 false')
    updated = modelrouteadmin.set_enabled(route_id, enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail='路由不存在')
    security.audit(user, 'set_model_route_enabled', updated['public_id'],
                   f"id={updated['id']}；enabled={enabled}；来源 {client_ip(request)}")
    return updated


@router.get('/settings/model-routes/config')
def get_model_routes_config(user: dict = Depends(security.current_user)) -> dict:
    return {'enabled': modelrouteadmin.switch_enabled()}


@router.post('/settings/model-routes/config')
def save_model_routes_config(body: dict, request: Request,
                             user: dict = Depends(security.require_session_admin)) -> dict:
    enabled = body.get('enabled')
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail='enabled 只能是 true 或 false')
    value = modelrouteadmin.set_switch(enabled)
    security.audit(user, 'update_model_routes_switch', '',
                   f'enabled={value}；来源 {client_ip(request)}')
    return {'enabled': value}


# ── GitHub Token（版本检测 / 一键更新）────────────────────
@router.get('/settings/github-token')
def get_github_token(user: dict = Depends(security.require_admin)) -> dict:
    """读当前生效的 GitHub Token（**只回掩码**）。

    为什么只给管理员：它是凭据，能操作对应的 GitHub 账号配额；普通用户不需要它。
    环境变量里已有值时界面要说明「由部署注入、面板填写不生效」，否则用户会以为
    自己填的那份没保存（issue #205 的现场就是「没地方填、也不知道有没有生效」）。
    """
    env_token = next((str(os.environ.get(k) or '').strip()
                      for k in ('WB_GITHUB_TOKEN', 'GITHUB_TOKEN')
                      if (os.environ.get(k) or '').strip()), '')
    stored = str(db.get_setting('github_token', '') or '').strip()
    effective = env_token or stored
    return {
        'configured': bool(effective),
        'masked': updater.mask_token(effective),
        'source': 'env' if env_token else ('panel' if stored else ''),
    }


@router.put('/settings/github-token')
def save_github_token(body: dict, user: dict = Depends(security.require_admin)) -> dict:
    """保存面板这份 GitHub Token（空串 = 清除）。

    不做格式校验（GitHub 的 token 形态换过几次），但长度上限兜住误贴整段文本；
    保存后不返回明文 —— 与其它凭据字段同一口径。
    """
    raw = str((body or {}).get('token') or '').strip()
    if len(raw) > 512:
        raise HTTPException(status_code=400, detail='GitHub Token 过长（最多 512 字符）')
    db.set_setting('github_token', raw)
    security.audit(user, 'update_github_token', '', '已配置' if raw else '已清除')
    return {'configured': bool(raw), 'masked': updater.mask_token(raw)}


# ── GitHub 镜像（issue #216）─────────────────────────────────
@router.get('/settings/github-mirror')
def get_github_mirror(user: dict = Depends(security.require_admin)) -> dict:
    """当前生效的 GitHub 镜像基址 + 来源（面板 / 环境变量 / 未配置）。"""
    env_value = updater.normalize_mirror(os.environ.get('WB_GITHUB_MIRROR') or '')
    stored = updater.stored_github_mirror()
    effective = updater.normalize_mirror(stored or env_value)
    return {
        'mirror': effective,
        'stored': stored,
        'env_mirror': env_value,
        'source': 'panel' if stored else ('env' if env_value else ''),
        'presets': ['https://gh-proxy.com/', 'https://gh-proxy.org/'],
    }


@router.put('/settings/github-mirror')
def save_github_mirror(body: dict, user: dict = Depends(security.require_admin)) -> dict:
    """保存镜像基址（空串 = 清除，回落到环境变量）。

    只接受「协议 + 主机 + 可选路径」：拼地址的规则是「基址 + 完整原始地址」，
    带查询串或片段的写法拼出来必然是一个错误页，所以在入口就拒掉。
    """
    raw = str((body or {}).get('mirror') or '').strip()
    if not raw:
        db.set_setting('github_mirror', '')
        security.audit(user, 'update_github_mirror', '', '已清除')
        return get_github_mirror(user)
    value = updater.normalize_mirror(raw)
    if not value:
        raise HTTPException(
            status_code=400,
            detail='镜像地址要以 http:// 或 https:// 开头，且不能带查询串或 # 片段',
        )
    db.set_setting('github_mirror', value)
    security.audit(user, 'update_github_mirror', '', value)
    return get_github_mirror(user)


# ── 管理端用户 ───────────────────────────────────────────
class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1)
    role: str = Field(pattern='^(admin|viewer)$')


class UserPatch(BaseModel):
    password: str | None = None
    role: str | None = Field(default=None, pattern='^(admin|viewer)$')


def _public(cfg: dict) -> list[dict]:
    return [{'username': u['username'], 'role': u.get('role', 'viewer')} for u in cfg.get('users', [])]


@router.get('/users')
def list_users(user: dict = Depends(security.current_user)) -> list[dict]:
    return _public(security.load_users())


@router.post('/users')
def add_user(body: UserIn, request: Request,
             user: dict = Depends(security.require_session_admin)) -> dict:
    """新建管理用户。

    审计是必须的：上次入侵里攻击者正是「先建自己的账号、再正常登录」，
    而这一步当时没有任何记录。新建用户写随机 sv，避免同名重建后旧 cookie 复活。
    """
    pw = str(body.password)
    problem = security.password_problem(pw, body.username)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    cfg = security.load_users()
    if any(u.get('username') == body.username for u in cfg.get('users', [])):
        raise HTTPException(status_code=409, detail='用户名已存在')
    cfg.setdefault('users', []).append({
        'username': body.username,
        'role': body.role,
        'pwd_hash': security.make_hash(pw),
        # 随机初始 sv：删除同名用户再重建时，不会让旧会话「复活」
        'sv': secrets.randbelow(1 << 30),
    })
    security.save_users(cfg)
    ip = client_ip(request)
    security.audit(user, 'add_user', body.username,
                   f'角色={body.role}；来源 {ip}', ip=ip)
    return {'username': body.username, 'role': body.role}


@router.patch('/users/{username}')
def update_user(username: str, body: UserPatch, request: Request,
                user: dict = Depends(security.require_session_admin)) -> dict:
    """改密码 / 改角色。

    两种改动都必须**吊销该用户既有会话**：
      - 改密码：否则被盗会话在改密码后仍能用到过期（7 天）——本次事故里攻击者
        改掉 admin 密码后，真正管理员手里的旧会话不该还能管理后台
      - 改角色：权限必须立即生效，不能等 cookie 过期（载荷里的 role 只是快照）

    吊销按用户进行（递增其 sv），不影响其他已登录用户。
    """
    cfg = security.load_users()
    target = next((u for u in cfg.get('users', []) if u.get('username') == username), None)
    if not target:
        raise HTTPException(status_code=404, detail='用户不存在')

    changed_pwd = changed_role = False
    if body.password:
        pw = str(body.password)
        # 管理端是公网入口：弱口令等于没有防护，统一走与启动时相同的策略。
        problem = security.password_problem(pw, username)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        target['pwd_hash'] = security.make_hash(pw)
        changed_pwd = True
    if body.role and body.role != target.get('role'):
        admins = [u for u in cfg.get('users', []) if u.get('role') == 'admin']
        if target.get('role') == 'admin' and len(admins) <= 1:
            raise HTTPException(status_code=400, detail='至少保留一个管理员')
        target['role'] = body.role
        changed_role = True

    if changed_pwd or changed_role:
        try:
            target['sv'] = int(target.get('sv') or 0) + 1
        except (TypeError, ValueError):
            target['sv'] = 1

    security.save_users(cfg)
    if changed_pwd and username == 'admin':
        # 改掉初始密码后，把 issue #179 留的那份明文副本抹掉 ——
        # 这里只存了初始 admin 密码，改其他用户的密码不应让它失效。
        security.clear_initial_password_file()
    if changed_pwd or changed_role:
        parts = []
        if changed_pwd:
            parts.append('改密码')
        if changed_role:
            parts.append('改角色')
        ip = client_ip(request)
        security.audit(user, 'update_user', username,
                       '、'.join(parts) + f'；来源 {ip}', ip=ip)
    # 把自己的密码改了，当前会话也会失效——前端据此提示重新登录
    self_revoked = bool(changed_pwd and username == user.get('username'))
    return {
        'username': target['username'],
        'role': target.get('role', 'viewer'),
        'sessions_revoked': changed_pwd or changed_role,
        'relogin_required': self_revoked,
    }


@router.delete('/users/{username}')
def delete_user(username: str, request: Request,
                user: dict = Depends(security.require_session_admin)) -> dict:
    cfg = security.load_users()
    users = cfg.get('users', [])
    target = next((u for u in users if u.get('username') == username), None)
    if not target:
        raise HTTPException(status_code=404, detail='用户不存在')
    admins = [u for u in users if u.get('role') == 'admin']
    if target.get('role') == 'admin' and len(admins) <= 1:
        raise HTTPException(status_code=400, detail='至少保留一个管理员')
    if target.get('username') == user.get('username'):
        raise HTTPException(status_code=400, detail='不能删除当前登录用户')
    cfg['users'] = [u for u in users if u.get('username') != username]
    security.save_users(cfg)
    if target.get('username') == 'admin':
        # 被删掉的就是初始账号，那份明文初始密码也随之失效。
        security.clear_initial_password_file()
    security.audit(user, 'delete_user', username, f'来源 {client_ip(request)}')
    return {'ok': True}


@router.get('/audit-logs')
def audit_logs(
    limit: int = 200,
    offset: int = 0,
    user: dict = Depends(security.require_admin),
) -> dict:
    """管理端审计日志（登录、改密码、增删用户、改安全配置等）。

    仅管理员可读：里面含用户名与来源 IP，属于敏感信息。
    """
    return {
        'items': db.list_audit_logs(limit=limit, offset=offset),
        'total': db.count_audit_logs(),
    }
