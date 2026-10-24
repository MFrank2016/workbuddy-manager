'use client';

/**
 * 工具端点开关（迁移 Todo 29，契约 §4）：`/v1/search` 与 `/v1/mcp`。
 *
 * 两个开关是**双写**的：manager 的 settings 决定代理是否放行，core 的
 * `search.enabled` / `mcp.enabled` 决定端点是否存在。只写一边就会出现
 * 「manager 放行但 core 404」或反之，用户看到的是随机 404。
 *
 * 非默认分组：core 的配置在各自实例上，面板只写默认 upstream。存在非默认分组时
 * 后端返回 `core_writable=false`，这里禁用开关并明确提示去哪儿改。
 */
import {useCallback, useEffect, useState} from 'react';
import {Loader2, Save, TriangleAlert, Wrench} from 'lucide-react';

import {errText, settingsApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {ToolsSettings} from '@/lib/types';
import {Button} from '@/components/ui/button';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';

export function ToolsPanel() {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [data, setData] = useState<ToolsSettings | null>(null);
  const [draft, setDraft] = useState<ToolsSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await settingsApi.tools();
      setData(next);
      setDraft(next);
      setLoadError(false);
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = !!data && !!draft &&
    (data.search_enabled !== draft.search_enabled || data.mcp_enabled !== draft.mcp_enabled);
  const disabled = !isAdmin || !draft?.core_writable;

  async function save() {
    if (!draft || disabled) return;
    setSaving(true);
    try {
      const next = await settingsApi.saveTools({
        search_enabled: draft.search_enabled,
        mcp_enabled: draft.mcp_enabled,
      });
      setData(next);
      setDraft(next);
      notify.ok(t('settings.toolsSaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-[20px] bg-muted px-3.5 py-3 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        {t('common.loading')}
      </div>
    );
  }

  if (loadError || !draft) {
    return (
      <div className="flex items-start gap-2.5 rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4 text-xs">
        <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
        <div>{t('settings.toolsLoadError')}</div>
      </div>
    );
  }

  return (
    <div className="rounded-[20px] bg-muted px-3.5 py-3">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Wrench className="h-4 w-4" />
            {t('settings.toolsTitle')}
          </div>
          <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
            {t('settings.toolsDesc')}
          </p>
        </div>
        {!isAdmin && (
          <span className="shrink-0 text-[11px] text-muted-foreground">
            {t('settings.toolsReadonly')}
          </span>
        )}
      </div>

      {!draft.core_writable && (
        <div className="mb-3 flex items-start gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px]">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
          <div>
            {t('settings.toolsNonDefaultHint', {
              groups: draft.non_default_upstreams.join(', ') || '-',
            })}
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="flex items-start justify-between gap-3 rounded-xl bg-background px-3 py-2.5">
          <div className="min-w-0">
            <Label className="text-xs">{t('settings.toolsSearch')}</Label>
            <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
              {t('settings.toolsSearchDesc')}
            </p>
          </div>
          <Switch
            checked={draft.search_enabled}
            onCheckedChange={(v) => setDraft({...draft, search_enabled: v})}
            disabled={disabled}
            aria-label={t('settings.toolsSearch')}
          />
        </div>
        <div className="flex items-start justify-between gap-3 rounded-xl bg-background px-3 py-2.5">
          <div className="min-w-0">
            <Label className="text-xs">{t('settings.toolsMcp')}</Label>
            <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
              {t('settings.toolsMcpDesc')}
            </p>
          </div>
          <Switch
            checked={draft.mcp_enabled}
            onCheckedChange={(v) => setDraft({...draft, mcp_enabled: v})}
            disabled={disabled}
            aria-label={t('settings.toolsMcp')}
          />
        </div>
      </div>

      <p className="mt-3 text-[11px] leading-4 text-muted-foreground">
        {t('settings.toolsCostHint')}
      </p>

      <div className="mt-3 flex items-center justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={() => setDraft(data)} disabled={!dirty || saving}>
          {t('common.cancel')}
        </Button>
        <Button size="sm" onClick={save} disabled={!dirty || disabled || saving}>
          {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Save className="mr-1.5 h-3.5 w-3.5" />}
          {t('common.save')}
        </Button>
      </div>
    </div>
  );
}
