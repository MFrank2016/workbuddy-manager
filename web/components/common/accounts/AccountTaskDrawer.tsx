'use client';

import {useCallback, useEffect, useState} from 'react';
import {Loader2, RefreshCw} from 'lucide-react';

import {errText, panelApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {Account, PanelTask} from '@/lib/types';
import {Button} from '@/components/ui/button';
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer';

/**
 * 账号任务清单（core 面板 `GET /panel/api/accounts/{uid}/tasks` 的 manager 版）。
 *
 * 与 `AccountTaskDialog` 的分工：
 *   · 本抽屉是**逐项任务**：看进度、接受、领取、自动完成；
 *   · `AccountTaskDialog` 是上游脚本的一键执行（预览 / 领奖 / 做任务）。
 * 两者数据源不同，不互相替代。
 */
export function AccountTaskDrawer({
  account,
  upstreamId,
  open,
  onOpenChange,
  onFinished,
}: {
  account: Account | null;
  upstreamId: number | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 领取/自动完成后通知外部刷新账号列表（积分可能变了）。 */
  onFinished?: () => void;
}) {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [tasks, setTasks] = useState<PanelTask[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [acting, setActing] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!account) return;
    setLoading(true);
    try {
      const r = await panelApi.tasks(account.uid, upstreamId);
      setTasks(r.tasks || []);
      setFailed(false);
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, [account, upstreamId]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  async function run(key: string, fn: () => Promise<unknown>, okKey: string) {
    setActing(key);
    try {
      await fn();
      notify.ok(t(okKey));
      await load();
      onFinished?.();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setActing(null);
    }
  }

  const rows = tasks || [];
  const canAuto = (x: PanelTask) => !x.claimed && !x.locked && x.accept_status !== 'claimed';

  return (
    <Drawer open={open} onOpenChange={onOpenChange}>
      <DrawerContent>
        <DrawerHeader>
          <DrawerTitle>{t('panelTasks.title')}</DrawerTitle>
          <DrawerDescription>
            {account?.nickname || account?.uid || ''} · {t('panelTasks.desc')}
          </DrawerDescription>
        </DrawerHeader>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 pb-8 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-muted-foreground">
              {t('panelTasks.count', {n: rows.length})}
            </span>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => void load()} disabled={loading}>
                {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                {t('panelTasks.retry')}
              </Button>
              {isAdmin && (
                <Button size="sm" disabled={loading || !account || acting !== null}
                        onClick={() => account && void run(
                          'all',
                          () => panelApi.acceptAll(account.uid, upstreamId),
                          'panelTasks.acceptAllDone')}>
                  {acting === 'all' && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
                  {t('panelTasks.acceptAll')}
                </Button>
              )}
            </div>
          </div>

          {failed && (
            <div className="rounded-xl bg-amber-500/10 px-3 py-2 text-amber-700 dark:text-amber-400">
              {t('panelTasks.failed')}
            </div>
          )}
          {!failed && !loading && rows.length === 0 && (
            <div className="rounded-xl bg-muted px-3 py-2 text-muted-foreground">
              {t('panelTasks.empty')}
            </div>
          )}

          {rows.map((x) => (
            <div key={x.task_code} className="rounded-xl bg-muted px-3 py-2.5">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="break-words text-xs font-medium">
                    {x.title || x.task_code}
                  </div>
                  <div className="mt-0.5 break-words text-[11px] text-muted-foreground">
                    {x.task_desc || x.description || x.task_code}
                  </div>
                </div>
                <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
                  {x.current}/{x.target}
                </span>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                {x.claimed ? (
                  <span className="text-[11px] text-emerald-600 dark:text-emerald-400">
                    {t('panelTasks.statusClaimed')}
                  </span>
                ) : x.locked ? (
                  <span className="text-[11px] text-muted-foreground">{t('panelTasks.statusLocked')}</span>
                ) : x.claimable ? (
                  <span className="text-[11px] text-amber-600 dark:text-amber-400">
                    {t('panelTasks.statusClaimable')}
                  </span>
                ) : (
                  <span className="text-[11px] text-muted-foreground">{t('panelTasks.statusInProgress')}</span>
                )}
                {(x.credit || x.energy) ? (
                  <span className="text-[11px] text-muted-foreground">
                    {t('panelTasks.reward', {credit: x.credit || 0, energy: x.energy || 0})}
                  </span>
                ) : null}
                {isAdmin && !x.claimed && !x.locked && (
                  <>
                    {x.accept_status !== 'accepted' && x.accept_status !== 'completed' && (
                      <Button size="sm" variant="outline" disabled={acting !== null}
                              onClick={() => account && void run(
                                `accept:${x.task_code}`,
                                () => panelApi.accept(account.uid, [x.task_code], upstreamId),
                                'panelTasks.acceptDone')}>
                        {t('panelTasks.accept')}
                      </Button>
                    )}
                    {x.claimable && (
                      <Button size="sm" disabled={acting !== null}
                              onClick={() => account && void run(
                                `claim:${x.task_code}`,
                                () => panelApi.claim(account.uid, x.task_code, upstreamId),
                                'panelTasks.claimDone')}>
                        {acting === `claim:${x.task_code}` && (
                          <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                        )}
                        {t('panelTasks.claim')}
                      </Button>
                    )}
                    {canAuto(x) && (
                      <Button size="sm" variant="outline" disabled={acting !== null}
                              onClick={() => account && void run(
                                `auto:${x.task_code}`,
                                () => panelApi.auto(account.uid, x.task_code, upstreamId),
                                'panelTasks.autoDone')}>
                        {acting === `auto:${x.task_code}` && (
                          <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                        )}
                        {t('panelTasks.auto')}
                      </Button>
                    )}
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      </DrawerContent>
    </Drawer>
  );
}
