'use client';

/**
 * Responses 投影 / 工具参数修复设置（迁移 Todo 16，契约 §5）。
 *
 * 这四个键是 **manager 本地设置**（db settings），不是 core config.json：
 * Responses 端点在本服务里实现，投影与工具参数修复也在这里做。
 *
 * 为什么单独一块而不是塞进「上游配置」的通用字段：那批字段保存的是
 * `config.json` 里的 core 段，而这里的值走 `/api/settings/responses`；
 * 混在一起会让人以为改的是上游行为。
 *
 * 权限：GET 任何登录用户都能读（只读角色也要知道请求会不会被改写），
 * POST 只对会话管理员开放——只读角色看到禁用控件 + 一句说明。
 */
import {useCallback, useEffect, useState} from 'react';
import {Loader2, Save, Sparkles, TriangleAlert} from 'lucide-react';

import {errText, settingsApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {ResponsesSettings} from '@/lib/types';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Switch} from '@/components/ui/switch';

const MIN_BYTES = 256;

export function ResponsesPanel() {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [data, setData] = useState<ResponsesSettings | null>(null);
  const [draft, setDraft] = useState<ResponsesSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await settingsApi.responses();
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

  const dirty =
    !!data && !!draft && JSON.stringify(data) !== JSON.stringify(draft);

  const maxBytesValid =
    !!draft && (draft.max_item_bytes === 0 || draft.max_item_bytes >= MIN_BYTES);

  async function save() {
    if (!draft || !maxBytesValid) return;
    setSaving(true);
    try {
      const next = await settingsApi.saveResponses(draft);
      setData(next);
      setDraft(next);
      notify.ok(t('settings.responsesSaved'));
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
        <div>{t('settings.responsesLoadError')}</div>
      </div>
    );
  }

  return (
    <div className="rounded-[20px] bg-muted px-3.5 py-3">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Sparkles className="h-4 w-4" />
            {t('settings.responsesTitle')}
          </div>
          <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
            {t('settings.responsesDesc')}
          </p>
        </div>
        {!isAdmin && (
          <span className="shrink-0 text-[11px] text-muted-foreground">
            {t('settings.responsesReadonly')}
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="space-y-1.5">
          <Label className="text-xs">{t('settings.responsesMode')}</Label>
          <Select
            value={draft.projection_mode}
            onValueChange={(v) =>
              setDraft({...draft, projection_mode: v as ResponsesSettings['projection_mode']})
            }
            disabled={!isAdmin}
          >
            <SelectTrigger className="h-8 bg-background text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="passthrough">
                {t('settings.responsesModePassthrough')}
              </SelectItem>
              <SelectItem value="balanced">
                {t('settings.responsesModeBalanced')}
              </SelectItem>
            </SelectContent>
          </Select>
          <p className="text-[11px] leading-4 text-muted-foreground">
            {t('settings.responsesModeDesc')}
          </p>
        </div>

        <div className="space-y-1.5">
          <Label className="text-xs">{t('settings.responsesMaxBytes')}</Label>
          <Input
            type="number"
            min={0}
            step={1}
            value={String(draft.max_item_bytes)}
            disabled={!isAdmin}
            onChange={(e) => {
              const n = Number(e.target.value);
              setDraft({...draft, max_item_bytes: Number.isFinite(n) ? Math.trunc(n) : 0});
            }}
            className="h-8 bg-background text-xs"
          />
          <p className="text-[11px] leading-4 text-muted-foreground">
            {t('settings.responsesMaxBytesDesc')}
          </p>
          {!maxBytesValid && (
            <p className="text-[11px] text-amber-600 dark:text-amber-400">
              {t('settings.responsesMaxBytesInvalid', {min: MIN_BYTES})}
            </p>
          )}
        </div>
      </div>

      <div className="mt-3 space-y-2">
        <label className="flex items-center justify-between gap-3 text-xs">
          <span>
            <span className="font-medium">{t('settings.responsesToolRepair')}</span>
            <span className="mt-0.5 block text-[11px] leading-4 text-muted-foreground">
              {t('settings.responsesToolRepairDesc')}
            </span>
          </span>
          <Switch
            checked={draft.tool_repair}
            disabled={!isAdmin}
            onCheckedChange={(v) => setDraft({...draft, tool_repair: v})}
          />
        </label>
        <label className="flex items-center justify-between gap-3 text-xs">
          <span>
            <span className="font-medium">{t('settings.responsesRecall')}</span>
            <span className="mt-0.5 block text-[11px] leading-4 text-muted-foreground">
              {t('settings.responsesRecallDesc')}
            </span>
          </span>
          <Switch
            checked={draft.tool_repair_recall}
            disabled={!isAdmin || !draft.tool_repair}
            onCheckedChange={(v) => setDraft({...draft, tool_repair_recall: v})}
          />
        </label>
      </div>

      <p className="mt-3 rounded-xl bg-amber-500/10 px-3 py-2 text-[11px] leading-4 text-amber-700 dark:text-amber-400">
        {t('settings.responsesCostHint')}
      </p>

      {isAdmin && (
        <div className="mt-3 flex justify-end">
          <Button
            size="sm"
            onClick={() => void save()}
            disabled={!dirty || !maxBytesValid || saving}
          >
            {saving ? (
              <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
            ) : (
              <Save className="mr-1.5 h-3.5 w-3.5" />
            )}
            {t('common.save')}
          </Button>
        </div>
      )}
    </div>
  );
}
