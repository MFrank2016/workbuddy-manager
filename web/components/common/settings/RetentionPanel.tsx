'use client';

import {useCallback, useEffect, useState} from 'react';
import {Database, Loader2, RotateCcw, Save} from 'lucide-react';
import {errText, settingsApi} from '@/lib/api';
import type {LogRetention, RetentionKey} from '@/lib/types';
import {
  diffDraft,
  isDraftValid,
  toDraft,
  type Draft,
} from '@/lib/retention-draft';
import {notify} from '@/lib/toast';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';

/**
 * 日志保留策略（迁移 C2：`log_retention_overrides`）。
 *
 * 五个数字输入，值来自后端的 `effective`（默认值 + 用户改过的键）。
 * 稀疏提交、`ip_max_rows` 的显式默认值、本地校验这三条规则都在
 * `@/lib/retention-draft`（纯函数，有 Node 行为测试）——组件只负责取数与渲染。
 *
 * 权限：GET 任何登录用户都能读（只读角色也要知道日志留多久），POST 只对会话
 * 管理员开放。只读角色看到输入框禁用 + 一句说明，而不是一次必然 403 的请求。
 */

/** 顺序与后端 `retention.SPECS` 一致；标签/说明走 i18n。 */
const FIELDS: {key: RetentionKey; labelKey: string; descKey: string}[] = [
  {key: 'request_days', labelKey: 'settings.retentionRequestDays',
   descKey: 'settings.retentionRequestDaysDesc'},
  {key: 'hourly_days', labelKey: 'settings.retentionHourlyDays',
   descKey: 'settings.retentionHourlyDaysDesc'},
  {key: 'task_days', labelKey: 'settings.retentionTaskDays',
   descKey: 'settings.retentionTaskDaysDesc'},
  {key: 'audit_max_rows', labelKey: 'settings.retentionAuditRows',
   descKey: 'settings.retentionAuditRowsDesc'},
  {key: 'ip_max_rows', labelKey: 'settings.retentionIpRows',
   descKey: 'settings.retentionIpRowsDesc'},
];

export function RetentionPanel() {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [data, setData] = useState<LogRetention | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  /** 用户**动过**的键（不是「值变了的键」——见 retention-draft 顶部第 1、2 条）。 */
  const [touched, setTouched] = useState<Set<RetentionKey>>(new Set());
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);

  const apply = useCallback((next: LogRetention) => {
    setData(next);
    setDraft(toDraft(next.effective));
    setTouched(new Set());
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      apply(await settingsApi.logRetention());
      setLoadError(false);
    } catch {
      // 取不到就如实说「取不到」，不要把空表单当成「当前配置」渲染出来。
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [apply]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-[20px] bg-muted p-4 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t('common.loading')}
      </div>
    );
  }

  if (loadError || !data || !draft) {
    return (
      <div className="rounded-[20px] bg-muted p-4 text-sm">
        <div className="font-medium">{t('settings.retentionLoadFailed')}</div>
        <Button variant="outline" className="mt-3 rounded-full" onClick={() => void load()}>
          {t('state.retry')}
        </Button>
      </div>
    );
  }

  const patch = diffDraft(draft, data.effective, touched);
  const dirty = Object.keys(patch).length > 0;
  const valid = isDraftValid(draft);

  const save = async () => {
    if (!dirty) {
      notify.info(t('settings.retentionNoChanges'));
      return;
    }
    setSaving(true);
    try {
      apply(await settingsApi.saveLogRetention(patch));
      notify.ok(t('settings.retentionSaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-[20px] bg-muted p-4">
      <div className="mb-1 flex items-center gap-2 text-sm font-medium">
        <Database className="h-4 w-4" />
        {t('settings.retentionTitle')}
      </div>
      <div className="mb-3 text-[11px] leading-5 text-muted-foreground">
        {t('settings.retentionDesc')}
      </div>
      {!isAdmin && (
        <div className="mb-3 text-[11px] text-muted-foreground">
          {t('settings.retentionReadonly')}
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {FIELDS.map(({key, labelKey, descKey}) => {
          const {min, max} = data.bounds[key];
          return (
            <div key={key} className="space-y-1.5">
              <Label
                htmlFor={`retention-${key}`}
                className="flex items-center gap-1.5 text-[11px] text-muted-foreground"
              >
                {t(labelKey)}
                {data.overrides[key] !== undefined && (
                  <span className="rounded-full bg-background px-1.5 text-[10px]">
                    {t('settings.retentionCustomized')}
                  </span>
                )}
              </Label>
              <Input
                id={`retention-${key}`}
                type="number"
                inputMode="numeric"
                min={min}
                max={max}
                step={1}
                disabled={!isAdmin || saving}
                value={draft[key]}
                aria-label={t(labelKey)}
                className="bg-background"
                onChange={(e) => {
                  setDraft({...draft, [key]: e.target.value});
                  setTouched((prev) => new Set(prev).add(key));
                }}
              />
              <div className="text-[10px] text-muted-foreground">
                {t(descKey)} · {t('settings.retentionRange', {min, max})}
              </div>
            </div>
          );
        })}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button
          className="rounded-full"
          disabled={!isAdmin || saving || !dirty || !valid}
          onClick={() => void save()}
        >
          {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          {t('common.save')}
        </Button>
        <Button
          variant="outline"
          className="rounded-full"
          disabled={!isAdmin || saving || !dirty}
          onClick={() => {
            setDraft(toDraft(data.effective));
            setTouched(new Set());
          }}
        >
          <RotateCcw className="h-4 w-4" />
          {t('settings.revert')}
        </Button>
        {!valid && (
          <span className="text-[11px] text-muted-foreground">
            {t('settings.retentionInvalid')}
          </span>
        )}
      </div>
    </div>
  );
}
