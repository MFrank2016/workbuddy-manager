'use client';

import {useCallback, useState} from 'react';
import {ChevronDown, Loader2} from 'lucide-react';

import {errText, panelApi} from '@/lib/api';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {
  PanelPackages,
  PanelSchoolVouchers,
  PanelUsageRemaining,
} from '@/lib/types';

/**
 * core 面板的只读观测能力（迁移 Phase 2）：
 *   · 积分构成 packages（逐账号积分包）
 *   · 开学季券码 school/vouchers
 *   · 窗口用量/剩余估算 usage/remaining
 *
 * 三段都是 `<details>` 懒加载：打开才打上游。packages/vouchers 会对每个账号
 * 扇出请求，放在页面首屏自动加载会明显拖慢账号页。
 */
export function PanelObservabilityPanel({upstreamId}: {upstreamId: number | null}) {
  const {t} = useI18n();
  const [packages, setPackages] = useState<PanelPackages | null>(null);
  const [vouchers, setVouchers] = useState<PanelSchoolVouchers | null>(null);
  const [usage, setUsage] = useState<PanelUsageRemaining | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const [failed, setFailed] = useState<Record<string, boolean>>({});

  const load = useCallback(async (kind: 'packages' | 'vouchers' | 'usage') => {
    if (loading === kind) return;
    setLoading(kind);
    try {
      if (kind === 'packages') setPackages(await panelApi.packages(upstreamId));
      else if (kind === 'vouchers') setVouchers(await panelApi.schoolVouchers(upstreamId));
      else setUsage(await panelApi.usageRemaining(24, upstreamId));
      setFailed((f) => ({...f, [kind]: false}));
    } catch (e) {
      setFailed((f) => ({...f, [kind]: true}));
      notify.err(errText(e));
    } finally {
      setLoading(null);
    }
  }, [loading, upstreamId]);

  function onToggle(kind: 'packages' | 'vouchers' | 'usage', open: boolean) {
    if (!open) return;
    const has = kind === 'packages' ? packages : kind === 'vouchers' ? vouchers : usage;
    if (!has && loading !== kind) void load(kind);
  }

  return (
    <section className="space-y-2">
      <details className="rounded-[20px] bg-muted px-4 py-3"
               onToggle={(e) => onToggle('packages', e.currentTarget.open)}>
        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-sm font-medium">
          <span>{t('panelObs.packages')}</span>
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
        </summary>
        <div className="mt-3 space-y-2 text-[11px]">
          {loading === 'packages' && <Loading />}
          {failed.packages && <Failed />}
          {packages && packages.accounts.length === 0 && <Empty />}
          {(packages?.accounts || []).map((a) => (
            <div key={a.uid} className="rounded-xl bg-background px-3 py-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">{a.nickname || a.uid}</span>
                <span className="text-muted-foreground">
                  {t('panelObs.remain')} {a.remain} / {a.size} · {t('panelObs.packageCount', {n: a.packages.length})}
                </span>
              </div>
              {a.error ? (
                <div className="mt-1 text-amber-600 dark:text-amber-400">{a.error}</div>
              ) : (
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {a.packages.slice(0, 6).map((p, i) => (
                    <span key={`${a.uid}-${i}`} className="rounded-md bg-muted px-1.5 py-0.5 text-[10px]">
                      {p.name || p.package_code || '—'}: {p.remain}/{p.size}
                      {p.end_time ? ` · ${p.end_time}` : ''}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </details>

      <details className="rounded-[20px] bg-muted px-4 py-3"
               onToggle={(e) => onToggle('vouchers', e.currentTarget.open)}>
        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-sm font-medium">
          <span>{t('panelObs.vouchers')}</span>
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
        </summary>
        <div className="mt-3 space-y-2 text-[11px]">
          {loading === 'vouchers' && <Loading />}
          {failed.vouchers && <Failed />}
          {vouchers && vouchers.accounts.length === 0 && <Empty />}
          {(vouchers?.accounts || []).map((a) => (
            <div key={a.uid} className="rounded-xl bg-background px-3 py-2">
              <div className="font-medium">{a.nickname || a.uid}</div>
              {a.error ? (
                <div className="mt-1 text-muted-foreground">{a.error}</div>
              ) : a.vouchers.length === 0 ? (
                <div className="mt-1 text-muted-foreground">{t('panelObs.empty')}</div>
              ) : (
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {a.vouchers.map((v) => (
                    <span key={v.grant_id} className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-[10px]"
                          title={`${v.prize_name || v.sku_code || ''} ${v.valid_to || ''}`}>
                      {v.prize_name || v.sku_code || t('panelObs.voucherCode')}: {v.code}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </details>

      <details className="rounded-[20px] bg-muted px-4 py-3"
               onToggle={(e) => onToggle('usage', e.currentTarget.open)}>
        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-sm font-medium">
          <span>{t('panelObs.usage')}</span>
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
        </summary>
        <div className="mt-3 space-y-2 text-[11px]">
          {loading === 'usage' && <Loading />}
          {failed.usage && <Failed />}
          {usage && usage.rows.length === 0 && <Empty />}
          {(usage?.rows || []).slice(0, 20).map((r) => (
            <div key={`${r.uid}-${r.model}`} className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-background px-3 py-2">
              <span className="font-medium">{r.nickname || r.uid} · {r.model}</span>
              <span className="text-muted-foreground">
                {t('panelObs.used')} {r.used}
                {r.budget != null ? ` / ${r.budget}` : ''}
                {r.remaining != null ? ` · ${t('panelObs.remaining')} ${r.remaining}` : ''}
                {r.cooling ? ` · ${t('panelObs.cooling')}` : ''}
                {r.estimated ? ` · ${t('panelObs.estimated')}` : ''}
              </span>
            </div>
          ))}
        </div>
      </details>
    </section>
  );
}

function Loading() {
  return (
    <div className="flex items-center gap-2 text-muted-foreground">
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      …
    </div>
  );
}

function Failed() {
  const {t} = useI18n();
  return <div className="text-amber-600 dark:text-amber-400">{t('panelObs.failed')}</div>;
}

function Empty() {
  const {t} = useI18n();
  return <div className="text-muted-foreground">{t('panelObs.empty')}</div>;
}
