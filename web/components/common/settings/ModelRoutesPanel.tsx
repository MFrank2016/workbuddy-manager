'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {Loader2, Pencil, Plus, Route, Trash2} from 'lucide-react';
import {errText, settingsApi, upstreamsApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {RichText} from '@/lib/i18n/rich-text';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {ModelRoute, UpstreamEndpoint} from '@/lib/types';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';

/**
 * 「设置 → 模型映射 → 模型路由」：表格编辑器 + 总开关（迁移 Todo 6，契约 §2）。
 *
 * 这里编辑的是**新的路由表**，与上方旧的「别名 → 模型名」两列映射是两套东西：
 * 路由多了 realm / 上游分组 / `keep_original` / `credits_override` 四个维度，解析
 * 走固定 6 级链（见 `server/services/modelroutes.py`）。**总开关默认关**，关着时
 * 网关逐字走旧映射——所以这个面板在开关关着时也要能完整编辑（先配好、再开启）。
 *
 * 刻意不提供的两样（契约要求）：
 *   · **priority** —— 解析链是固定的，没有优先级；
 *   · **账号绑定** —— 路由只按 `(public_id, realm, key_group)` 匹配，具体账号由
 *     密钥绑定的上游分组决定，面板不暴露、也不接受账号 id。
 *
 * 权限：GET 任何登录用户都能读（只读角色也要知道下游名字实际打到哪儿），写只对
 * 会话管理员开放。只读角色看到的是禁用控件 + 一句说明，而不是一次必然 403 的请求。
 */

/** realm 的取值与后端 `modelroutes._REALMS` 同口径；'' = 两版都适用。 */
const REALMS = ['', 'cn', 'global'] as const;

type Draft = {
  public_id: string;
  target_model: string;
  realm: string;
  group: string;          // '' = 任意分组；否则是 upstreams.id 的字符串
  keep_original: boolean;
  credits: string;        // '' = 不覆盖
  enabled: boolean;
};

const EMPTY: Draft = {
  public_id: '',
  target_model: '',
  realm: '',
  group: '',
  keep_original: true,
  credits: '',
  enabled: true,
};

function toDraft(route: ModelRoute): Draft {
  return {
    public_id: route.public_id,
    target_model: route.target_model,
    realm: route.realm,
    group: route.upstream_group_id == null ? '' : String(route.upstream_group_id),
    keep_original: route.keep_original,
    credits: route.credits_override == null ? '' : String(route.credits_override),
    enabled: route.enabled,
  };
}

/** 草稿 → 请求体。空串要变成 `null`，否则后端会把「留空」当成非法值。 */
function toBody(draft: Draft): Partial<ModelRoute> {
  const credits = draft.credits.trim();
  return {
    public_id: draft.public_id.trim(),
    target_model: draft.target_model.trim(),
    realm: draft.realm,
    upstream_group_id: draft.group ? Number(draft.group) : null,
    keep_original: draft.keep_original,
    credits_override: credits === '' ? null : Number(credits),
    enabled: draft.enabled,
  };
}

export function ModelRoutesPanel() {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [routes, setRoutes] = useState<ModelRoute[]>([]);
  const [switchOn, setSwitchOn] = useState(false);
  const [groups, setGroups] = useState<UpstreamEndpoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [editing, setEditing] = useState<number | null>(null);

  const apply = useCallback((next: {items: ModelRoute[]; enabled: boolean}) => {
    setRoutes(next.items);
    setSwitchOn(next.enabled);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      apply(await settingsApi.modelRoutes());
      setLoadError(false);
    } catch {
      // 取不到就如实说取不到，不要把空表当成「当前没有路由」渲染出来。
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [apply]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    // 分组下拉只有管理员能读（接口是登录即可，但非管理员不必拉）
    if (!isAdmin) return;
    void upstreamsApi.list()
      .then((r) => setGroups(r.items.filter((g) => g.enabled && g.id != null)))
      .catch(() => setGroups([]));
  }, [isAdmin]);

  const groupName = useMemo(() => {
    const map = new Map<number, string>();
    for (const g of groups) if (g.id != null) map.set(g.id, g.name);
    return map;
  }, [groups]);

  async function toggleSwitch(next: boolean) {
    setSaving(true);
    try {
      const r = await settingsApi.saveModelRoutesConfig(next);
      setSwitchOn(r.enabled);
      notify.ok(t('settings.routesSaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  async function submit() {
    if (!draft) return;
    const body = toBody(draft);
    if (!body.public_id) {
      notify.warn(t('settings.routesPublicIdRequired'));
      return;
    }
    if (!body.target_model) {
      notify.warn(t('settings.routesTargetRequired'));
      return;
    }
    setSaving(true);
    try {
      if (editing !== null) await settingsApi.updateModelRoute(editing, body);
      else await settingsApi.createModelRoute(body);
      notify.ok(t('settings.routesSaved'));
      setDraft(null);
      setEditing(null);
      await load();
    } catch (e) {
      // 400 / 404 / 409 的 detail 由后端给（可照做的一句话），这里原样展示。
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  async function toggleRow(route: ModelRoute, next: boolean) {
    try {
      await settingsApi.setModelRouteEnabled(route.id, next);
      await load();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  async function remove(route: ModelRoute) {
    try {
      await settingsApi.removeModelRoute(route.id);
      notify.ok(t('settings.routesDeleted'));
      await load();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-[20px] bg-muted p-4 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t('common.loading')}
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="rounded-[20px] bg-muted p-4 text-sm">
        <div className="font-medium">{t('settings.routesLoadFailed')}</div>
        <Button variant="outline" className="mt-3 rounded-full" onClick={() => void load()}>
          {t('state.retry')}
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* ── 总开关 ── */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="mb-1 text-sm font-medium">{t('settings.routesSwitch')}</div>
            <div className="text-[11px] leading-4 text-muted-foreground">
              <RichText text={t('settings.routesSwitchDesc')} />
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2 pt-1">
            <span className="text-[11px] text-muted-foreground">
              {switchOn ? t('settings.routesSwitchOn') : t('settings.routesSwitchOff')}
            </span>
            <Switch checked={switchOn} disabled={!isAdmin || saving}
                    aria-label={t('settings.routesSwitch')}
                    onCheckedChange={(v) => void toggleSwitch(v)} />
          </div>
        </div>
      </div>

      {/* ── 新增 ── */}
      {isAdmin && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-1 text-sm font-medium">{t('settings.routesAdd')}</div>
          <div className="mb-3 text-[11px] leading-4 text-muted-foreground">
            <RichText text={t('settings.routesAddDesc')} />
          </div>
          <Button className="rounded-full" onClick={() => {
            setDraft({...EMPTY});
            setEditing(null);
          }}>
            <Plus />
            {t('settings.routesAdd')}
          </Button>
        </div>
      )}

      {/* ── 表格 ── */}
      <div className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">
                {t('settings.routesPublicId')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('settings.routesTarget')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('settings.routesRealm')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('settings.routesGroup')}
              </TableHead>
              <TableHead className="w-24 text-[11px] text-muted-foreground">
                {t('settings.routesColStatus')}
              </TableHead>
              {isAdmin && (
                <TableHead className="w-28 pr-4 text-right text-[11px] text-muted-foreground">
                  {t('settings.routesColActions')}
                </TableHead>
              )}
            </TableRow>
          </TableHeader>
          <TableBody>
            {routes.map((route) => (
              <TableRow key={route.id} className="border-b border-border/40">
                <TableCell className="pl-4 font-mono text-xs">{route.public_id}</TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">
                  {route.target_model}
                  {route.keep_original && (
                    <span className="ml-1.5 font-sans text-[10px] text-muted-foreground/70">
                      +{t('settings.routesKeepOriginalShort')}
                    </span>
                  )}
                </TableCell>
                <TableCell className="text-[11px]">
                  {route.realm || t('settings.routesRealmAny')}
                </TableCell>
                <TableCell className="text-[11px]">
                  {route.upstream_group_id == null
                    ? t('settings.routesGroupAny')
                    : (groupName.get(route.upstream_group_id)
                       ?? `#${route.upstream_group_id}`)}
                </TableCell>
                <TableCell>
                  <div className="flex items-center gap-1.5">
                    <Switch checked={route.enabled} disabled={!isAdmin}
                            aria-label={t('settings.routesEnabled')}
                            onCheckedChange={(v) => void toggleRow(route, v)} />
                    {!route.enabled && (
                      <Badge variant="secondary" className="text-[10px]">
                        {t('settings.routesDisabledBadge')}
                      </Badge>
                    )}
                  </div>
                </TableCell>
                {isAdmin && (
                  <TableCell className="pr-4">
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md"
                              title={t('common.edit')}
                              onClick={() => {
                                setDraft(toDraft(route));
                                setEditing(route.id);
                              }}>
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <ConfirmDialog destructive
                                     title={t('settings.routesDeleteTitle',
                                              {name: route.public_id})}
                                     description={t('settings.routesDeleteDesc')}
                                     onConfirm={() => remove(route)}
                                     trigger={
                                       <Button variant="ghost" size="icon"
                                               className="h-7 w-7 rounded-md text-red-500 hover:text-red-600">
                                         <Trash2 className="h-3.5 w-3.5" />
                                       </Button>
                                     } />
                    </div>
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {!routes.length && (
          <EmptyState icon={Route} title={t('settings.routesEmpty')}
                      description={t('settings.routesEmptyDesc')}
                      className="flex flex-col items-center justify-center py-12 text-center" />
        )}
      </div>

      {/* ── 新增 / 编辑 ── */}
      <Dialog open={draft !== null}
              onOpenChange={(open) => {
                if (!open) {
                  setDraft(null);
                  setEditing(null);
                }
              }}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>
              {editing !== null ? t('settings.routesEdit') : t('settings.routesAdd')}
            </DialogTitle>
            <DialogDescription>{t('settings.routesDialogDesc')}</DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-2.5">
            {draft && (
              <>
                <div className="rounded-2xl bg-muted/40 px-3 py-2">
                  <Label className="text-xs">{t('settings.routesPublicId')}</Label>
                  <Input className="mt-1 h-8 bg-background font-mono text-xs"
                         value={draft.public_id}
                         onChange={(e) => setDraft({...draft, public_id: e.target.value})} />
                  <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                    {t('settings.routesPublicIdHint')}
                  </p>
                </div>
                <div className="rounded-2xl bg-muted/40 px-3 py-2">
                  <Label className="text-xs">{t('settings.routesTarget')}</Label>
                  <Input className="mt-1 h-8 bg-background font-mono text-xs"
                         value={draft.target_model}
                         onChange={(e) => setDraft({...draft, target_model: e.target.value})} />
                </div>
                <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                  <div className="rounded-2xl bg-muted/40 px-3 py-2">
                    <Label className="text-xs">{t('settings.routesRealm')}</Label>
                    <Select value={draft.realm || '__any__'}
                            onValueChange={(v) => setDraft({
                              ...draft, realm: v === '__any__' ? '' : v,
                            })}>
                      <SelectTrigger className="mt-1 h-8 bg-background" aria-label={t('settings.routesRealm')}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {REALMS.map((r) => (
                          <SelectItem key={r || '__any__'} value={r || '__any__'}>
                            {r === '' ? t('settings.routesRealmAny')
                              : r === 'cn' ? t('settings.routesRealmCn') : t('settings.routesRealmGlobal')}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="rounded-2xl bg-muted/40 px-3 py-2">
                    <Label className="text-xs">{t('settings.routesGroup')}</Label>
                    <Select value={draft.group || '__any__'}
                            onValueChange={(v) => setDraft({
                              ...draft, group: v === '__any__' ? '' : v,
                            })}>
                      <SelectTrigger className="mt-1 h-8 bg-background" aria-label={t('settings.routesGroup')}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__any__">{t('settings.routesGroupAny')}</SelectItem>
                        {groups.map((g) => (
                          <SelectItem key={g.id} value={String(g.id)}>{g.name}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </div>
                <div className="rounded-2xl bg-muted/40 px-3 py-2">
                  <Label className="text-xs">{t('settings.routesCredits')}</Label>
                  <Input className="mt-1 h-8 w-32 bg-background text-right tabular-nums"
                         value={draft.credits}
                         onChange={(e) => setDraft({...draft, credits: e.target.value})} />
                  <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                    {t('settings.routesCreditsHint')}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-5 rounded-2xl bg-muted/40 px-3 py-2">
                  <label className="flex items-center gap-2 text-xs">
                    <Switch checked={draft.keep_original}
                            aria-label={t('settings.routesKeepOriginal')}
                            onCheckedChange={(v) => setDraft({...draft, keep_original: v})} />
                    {t('settings.routesKeepOriginal')}
                  </label>
                  <label className="flex items-center gap-2 text-xs">
                    <Switch checked={draft.enabled}
                            aria-label={t('settings.routesEnabled')}
                            onCheckedChange={(v) => setDraft({...draft, enabled: v})} />
                    {t('settings.routesEnabled')}
                  </label>
                </div>
              </>
            )}
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" className="rounded-full"
                    onClick={() => {
                      setDraft(null);
                      setEditing(null);
                    }}>
              {t('common.cancel')}
            </Button>
            <Button className="rounded-full" disabled={saving} onClick={() => void submit()}>
              {t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
