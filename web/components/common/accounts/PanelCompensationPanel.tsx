'use client';

import {useCallback, useEffect, useState} from 'react';
import {Gift, Loader2, RefreshCw} from 'lucide-react';

import {Button} from '@/components/ui/button';
import {useAuth} from '@/lib/auth-context';
import {errText, panelApi} from '@/lib/api';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {PanelCompensation, PanelCompensationRow} from '@/lib/types';

/**
 * core 面板补偿状态（Phase 3）：只读展示 + 会话管理员领取。
 *
 * 补偿带截止时间，最怕「静默过期」：这里把可领额度、截止倒计时和跳过原因
 * 直接铺在账号页顶部，不用再进 core 面板才能看见。
 */
export function PanelCompensationPanel({
  upstreamId,
  onChanged,
}: {
  upstreamId: number | null;
  onChanged?: () => void;
}) {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [data, setData] = useState<PanelCompensation | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [acting, setActing] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await panelApi.compensation(upstreamId));
      setFailed(false);
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, [upstreamId]);

  useEffect(() => {
    void load();
  }, [load]);

  // 倒计时每分钟刷新即可；补偿截止通常按天/小时计。
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  const rows = data?.accounts || [];
  const pending = rows.filter(isPending);

  function isPending(row: PanelCompensationRow): boolean {
    return row.status.active && !row.status.claimed && row.status.credit_num > 0
      && !row.skip && !row.error;
  }

  function countdown(end?: string): string {
    if (!end) return '';
    // core 的 end_time 是 CST 墙钟（无时区）；补 +08:00，避免非中国时区
    // 的浏览器把截止时间当成本地时间、倒计时整体偏移。
    const iso = end.includes('T') ? end : end.replace(' ', 'T');
    const at = Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + '+08:00');
    if (!Number.isFinite(at)) return '';
    const left = at - now;
    if (left <= 0) return t('panelComp.expired');
    const days = Math.floor(left / 86_400_000);
    const hours = Math.floor((left % 86_400_000) / 3_600_000);
    const minutes = Math.floor((left % 3_600_000) / 60_000);
    if (days > 0) return t('panelComp.leftDays', {d: days, h: hours});
    if (hours > 0) return t('panelComp.leftHours', {h: hours, m: minutes});
    return t('panelComp.leftMinutes', {m: minutes});
  }

  async function claim(uid: string) {
    setActing(`one:${uid}`);
    try {
      const out = await panelApi.claimCompensation(uid, upstreamId);
      const credit = out.result?.credit || 0;
      if (out.result?.claimed) notify.ok(t('panelComp.claimDone', {credit}));
      else notify.info(t('panelComp.claimNothing'));
      await load();
      onChanged?.();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setActing(null);
    }
  }

  async function claimAll() {
    setActing('all');
    try {
      const out = await panelApi.claimAllCompensation(upstreamId);
      notify.ok(t('panelComp.claimAllDone', {
        claimed: out.summary?.claimed || 0,
        credit: out.summary?.credit || 0,
      }));
      await load();
      onChanged?.();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setActing(null);
    }
  }

  return (
    <section className="rounded-[20px] bg-muted px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-sm font-medium">
            <Gift className="h-4 w-4 text-muted-foreground" />
            {t('panelComp.title')}
          </div>
          <div className="mt-0.5 text-[11px] text-muted-foreground">
            {t('panelComp.desc')}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={() => void load()} disabled={loading || acting !== null}>
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            {t('panelComp.refresh')}
          </Button>
          {isAdmin && pending.length > 0 && (
            <Button size="sm" onClick={() => void claimAll()} disabled={acting !== null}>
              {acting === 'all' && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
              {t('panelComp.claimAll', {n: pending.length})}
            </Button>
          )}
        </div>
      </div>

      <div className="mt-3 space-y-2 text-[11px]">
        {failed && (
          <div className="rounded-xl bg-amber-500/10 px-3 py-2 text-amber-700 dark:text-amber-400">
            {t('panelComp.failed')}
          </div>
        )}
        {!failed && !loading && rows.length === 0 && (
          <div className="rounded-xl bg-background px-3 py-2 text-muted-foreground">
            {t('panelComp.empty')}
          </div>
        )}
        {rows.map((row) => {
          const st = row.status;
          const canClaim = isAdmin && isPending(row);
          return (
            <div key={row.uid} className="rounded-xl bg-background px-3 py-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">{row.nickname || row.uid}</span>
                <span className="flex min-w-0 flex-wrap items-center justify-end gap-2">
                  {row.error ? (
                    <span className="min-w-0 break-words text-rose-600 dark:text-rose-400">
                      {t('panelComp.error')}: {row.error}
                    </span>
                  ) : row.skip ? (
                    <span className="min-w-0 break-words text-muted-foreground">
                      {row.skip_msg || row.skip}
                    </span>
                  ) : st.active && !st.claimed && st.credit_num > 0 ? (
                    <span className="text-amber-600 dark:text-amber-400">
                      {t('panelComp.claimable', {credit: st.credit_num})}
                    </span>
                  ) : st.claimed ? (
                    <span className="text-emerald-600 dark:text-emerald-400">
                      {t('panelComp.claimed')}
                    </span>
                  ) : (
                    <span className="text-muted-foreground">{t('panelComp.noActivity')}</span>
                  )}
                  {st.end_time && st.active && !st.claimed && (
                    <span className="text-muted-foreground" title={t('panelComp.deadline', {at: st.end_time})}>
                      {countdown(st.end_time)}
                    </span>
                  )}
                  {canClaim && (
                    <Button size="sm" className="shrink-0" disabled={acting !== null}
                            onClick={() => void claim(row.uid)}>
                      {acting === `one:${row.uid}` && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
                      {t('panelComp.claim')}
                    </Button>
                  )}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
