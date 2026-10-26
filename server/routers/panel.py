"""core 内置面板能力的 manager 代理（迁移 Phase 1）。"""
from __future__ import annotations

from urllib.parse import quote, urlencode

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .. import security
from ..services import panelapi

router = APIRouter(prefix='/api', tags=['panel'])


def _raise(exc: panelapi.PanelAPIError) -> None:
    raise HTTPException(status_code=exc.status, detail=exc.message) from exc


def _admin(user: dict = Depends(security.require_session_admin)) -> dict:
    """写端点：只接受会话管理员（API Token 一律 403）。"""
    return user


def _uid(uid: str) -> str:
    return quote(uid, safe='')


@router.get('/panel/accounts/{uid}/tasks')
async def account_tasks(uid: str, upstream_id: int | None = Query(None),
                        user: dict = Depends(security.current_user)) -> dict:
    """代理 core `GET /panel/api/accounts/{uid}/tasks`。"""
    try:
        up = panelapi.resolve_upstream(upstream_id)
        return await panelapi.call(up, 'GET', f'/panel/api/accounts/{_uid(uid)}/tasks')
    except panelapi.PanelAPIError as exc:
        _raise(exc)


async def _write(uid: str, action: str, body: dict, upstream_id: int | None,
                 user: dict) -> dict:
    try:
        up = panelapi.resolve_upstream(upstream_id)
        out = await panelapi.call(
            up, 'POST', f'/panel/api/accounts/{_uid(uid)}/tasks/{action}', body)
    except panelapi.PanelAPIError as exc:
        _raise(exc)
    detail = ','.join(str(x) for x in (body.get('task_codes') or [body.get('task_code') or '']))
    security.audit(user, f'panel_task_{action}', uid, f'tasks={detail}')
    return out


@router.post('/panel/accounts/{uid}/tasks/accept')
async def account_task_accept(uid: str, body: dict = Body(...),
                              upstream_id: int | None = Query(None),
                              user: dict = Depends(_admin)) -> dict:
    codes = body.get('task_codes')
    if not isinstance(codes, list) or not codes:
        raise HTTPException(status_code=400, detail='task_codes 必须是非空数组')
    return await _write(uid, 'accept', {'task_codes': codes}, upstream_id, user)


@router.post('/panel/accounts/{uid}/tasks/accept_all')
async def account_task_accept_all(uid: str, upstream_id: int | None = Query(None),
                                  user: dict = Depends(_admin)) -> dict:
    return await _write(uid, 'accept_all', {}, upstream_id, user)


@router.post('/panel/accounts/{uid}/tasks/claim')
async def account_task_claim(uid: str, body: dict = Body(...),
                             upstream_id: int | None = Query(None),
                             user: dict = Depends(_admin)) -> dict:
    code = str(body.get('task_code') or '').strip()
    if not code:
        raise HTTPException(status_code=400, detail='task_code 必填')
    return await _write(uid, 'claim', {'task_code': code}, upstream_id, user)


@router.post('/panel/accounts/{uid}/tasks/auto')
async def account_task_auto(uid: str, body: dict = Body(...),
                            upstream_id: int | None = Query(None),
                            user: dict = Depends(_admin)) -> dict:
    code = str(body.get('task_code') or '').strip()
    if not code:
        raise HTTPException(status_code=400, detail='task_code 必填')
    return await _write(uid, 'auto', {'task_code': code}, upstream_id, user)


@router.post('/panel/accounts/{uid}/tasks/auto_all')
async def account_task_auto_all(uid: str, body: dict = Body(default={}),
                                upstream_id: int | None = Query(None),
                                user: dict = Depends(_admin)) -> dict:
    codes = body.get('task_codes') if isinstance(body, dict) else None
    if codes is not None and not isinstance(codes, list):
        raise HTTPException(status_code=400, detail='task_codes 必须是数组')
    return await _write(uid, 'auto_all', {'task_codes': codes or []}, upstream_id, user)


@router.post('/panel/tasks/scan_all')
async def tasks_scan_all(upstream_id: int | None = Query(None),
                         user: dict = Depends(_admin)) -> dict:
    try:
        up = panelapi.resolve_upstream(upstream_id)
        out = await panelapi.call(up, 'POST', '/panel/api/tasks/scan_all', {})
    except panelapi.PanelAPIError as exc:
        _raise(exc)
    security.audit(user, 'panel_tasks_scan_all', '', f'pending={out.get("pending_count")}')
    return out


@router.post('/panel/tasks/run_queue')
async def tasks_run_queue(body: dict = Body(default={}),
                          upstream_id: int | None = Query(None),
                          user: dict = Depends(_admin)) -> dict:
    conc = int(body.get('concurrency') or 1)
    conc = max(1, min(4, conc))
    payload = {'concurrency': conc, 'growth': body.get('growth', True) is not False}
    try:
        up = panelapi.resolve_upstream(upstream_id)
        out = await panelapi.call(up, 'POST', '/panel/api/tasks/run_queue', payload)
    except panelapi.PanelAPIError as exc:
        _raise(exc)
    security.audit(user, 'panel_tasks_run_queue', '', f'concurrency={conc}')
    return out


@router.get('/panel/tasks/queue')
async def tasks_queue(upstream_id: int | None = Query(None),
                      user: dict = Depends(security.current_user)) -> dict:
    try:
        up = panelapi.resolve_upstream(upstream_id)
        return await panelapi.call(up, 'GET', '/panel/api/tasks/queue')
    except panelapi.PanelAPIError as exc:
        _raise(exc)


@router.get('/panel/packages')
async def panel_packages(upstream_id: int | None = Query(None),
                         user: dict = Depends(security.current_user)) -> dict:
    """代理 core `GET /panel/api/packages`（逐账号积分包构成）。"""
    try:
        up = panelapi.resolve_upstream(upstream_id)
        return await panelapi.call(up, 'GET', '/panel/api/packages')
    except panelapi.PanelAPIError as exc:
        _raise(exc)


@router.get('/panel/overview')
async def panel_overview(upstream_id: int | None = Query(None),
                         user: dict = Depends(security.current_user)) -> dict:
    """代理 core `GET /panel/api/overview`（池计数 / 账号 / 模型锁，只读）。"""
    try:
        up = panelapi.resolve_upstream(upstream_id)
        return await panelapi.call(up, 'GET', '/panel/api/overview')
    except panelapi.PanelAPIError as exc:
        _raise(exc)


@router.get('/panel/request_metrics')
async def panel_request_metrics(upstream_id: int | None = Query(None),
                                user: dict = Depends(security.current_user)) -> dict:
    """代理 core `GET /panel/api/request_metrics`（请求指标快照，只读）。"""
    try:
        up = panelapi.resolve_upstream(upstream_id)
        return await panelapi.call(up, 'GET', '/panel/api/request_metrics')
    except panelapi.PanelAPIError as exc:
        _raise(exc)


@router.get('/panel/usage/remaining')
async def panel_usage_remaining(hours: int = Query(24), upstream_id: int | None = Query(None),
                                user: dict = Depends(security.current_user)) -> dict:
    """代理 core `GET /panel/api/usage/remaining`（窗口用量/剩余估算）。"""
    h = max(1, min(1440, int(hours or 24)))
    try:
        up = panelapi.resolve_upstream(upstream_id)
        return await panelapi.call(up, 'GET', f'/panel/api/usage/remaining?hours={h}')
    except panelapi.PanelAPIError as exc:
        _raise(exc)


@router.get('/panel/school/vouchers')
async def panel_school_vouchers(upstream_id: int | None = Query(None),
                                user: dict = Depends(security.current_user)) -> dict:
    """代理 core `GET /panel/api/school/vouchers`（开学季券码）。"""
    try:
        up = panelapi.resolve_upstream(upstream_id)
        return await panelapi.call(up, 'GET', '/panel/api/school/vouchers')
    except panelapi.PanelAPIError as exc:
        _raise(exc)


@router.get('/panel/model_probes')
async def panel_model_probes(upstream_id: int | None = Query(None),
                             user: dict = Depends(security.current_user)) -> dict:
    """代理 core `GET /panel/api/model_probes`（模型实测上限标注）。"""
    try:
        up = panelapi.resolve_upstream(upstream_id)
        return await panelapi.call(up, 'GET', '/panel/api/model_probes')
    except panelapi.PanelAPIError as exc:
        _raise(exc)


# ── Phase 3：补偿状态 / 积分历史 / 暂停选号 ─────────────────────────────


@router.get('/panel/compensation')
async def panel_compensation(upstream_id: int | None = Query(None),
                             user: dict = Depends(security.current_user)) -> dict:
    """代理 core `GET /panel/api/compensation`（逐账号补偿状态，只读）。"""
    try:
        up = panelapi.resolve_upstream(upstream_id)
        return await panelapi.call(up, 'GET', '/panel/api/compensation')
    except panelapi.PanelAPIError as exc:
        _raise(exc)


@router.post('/panel/compensation/claim-all')
async def panel_compensation_claim_all(upstream_id: int | None = Query(None),
                                       user: dict = Depends(_admin)) -> dict:
    """代理 core `POST /panel/api/compensation_all`（逐号查状态后领取）。"""
    try:
        up = panelapi.resolve_upstream(upstream_id)
        out = await panelapi.call(up, 'POST', '/panel/api/compensation_all', {})
    except panelapi.PanelAPIError as exc:
        _raise(exc)
    summary = out.get('summary') if isinstance(out.get('summary'), dict) else {}
    security.audit(
        user, 'panel_compensation_claim_all', '',
        f"claimed={summary.get('claimed', 0)} credit={summary.get('credit', 0)} "
        f"skipped={summary.get('skipped', 0)} errored={summary.get('errored', 0)}",
    )
    return out


@router.post('/panel/compensation/{uid}/claim')
async def panel_compensation_claim(uid: str, upstream_id: int | None = Query(None),
                                   user: dict = Depends(_admin)) -> dict:
    """代理 core `POST /panel/api/accounts/{uid}/compensation`（单号领取）。"""
    try:
        up = panelapi.resolve_upstream(upstream_id)
        out = await panelapi.call(
            up, 'POST', f'/panel/api/accounts/{_uid(uid)}/compensation', {})
    except panelapi.PanelAPIError as exc:
        _raise(exc)
    result = out.get('result') if isinstance(out.get('result'), dict) else {}
    security.audit(
        user, 'panel_compensation_claim', uid,
        f"claimed={bool(result.get('claimed'))} credit={result.get('credit', 0)} "
        f"skip={result.get('skip', '')} error={result.get('error', '')}",
    )
    return out


@router.get('/panel/credit_history')
async def panel_credit_history(uid: str | None = Query(None),
                               limit: int = Query(200),
                               upstream_id: int | None = Query(None),
                               user: dict = Depends(security.current_user)) -> dict:
    """代理 core `GET /panel/api/credit_history`（逐账号积分变动流水）。"""
    lim = max(1, min(1000, int(limit or 200)))
    params: dict[str, str] = {'limit': str(lim)}
    if uid:
        params['uid'] = uid
    try:
        up = panelapi.resolve_upstream(upstream_id)
        return await panelapi.call(up, 'GET', f'/panel/api/credit_history?{urlencode(params)}')
    except panelapi.PanelAPIError as exc:
        _raise(exc)


@router.post('/panel/accounts/{uid}/pause')
async def panel_account_pause(uid: str, upstream_id: int | None = Query(None),
                              user: dict = Depends(_admin)) -> dict:
    """代理 core `POST /panel/api/accounts/{uid}/pause`（暂停选号，保号任务照常）。"""
    try:
        up = panelapi.resolve_upstream(upstream_id)
        out = await panelapi.call(up, 'POST', f'/panel/api/accounts/{_uid(uid)}/pause', {})
    except panelapi.PanelAPIError as exc:
        _raise(exc)
    security.audit(user, 'panel_account_pause', uid)
    return out


@router.post('/panel/accounts/{uid}/resume')
async def panel_account_resume(uid: str, upstream_id: int | None = Query(None),
                               user: dict = Depends(_admin)) -> dict:
    """代理 core `POST /panel/api/accounts/{uid}/resume`（恢复参与选号）。"""
    try:
        up = panelapi.resolve_upstream(upstream_id)
        out = await panelapi.call(up, 'POST', f'/panel/api/accounts/{_uid(uid)}/resume', {})
    except panelapi.PanelAPIError as exc:
        _raise(exc)
    security.audit(user, 'panel_account_resume', uid)
    return out
