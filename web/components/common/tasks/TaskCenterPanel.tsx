'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {ListChecks, Loader2, Play, RefreshCw} from 'lucide-react';

import {errText, panelApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {PanelQueueStatus, PanelScanItem} from '@/lib/types';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';

/**
 * 任务中心（core 面板 `tasks/scan_all` + `tasks/run_queue` + `tasks/queue`）。
 *
 * 与 `TaskRunnerPanel` 的分工：后者跑上游 `task_runner.py`（预览/领奖/做任务），
 * 本面板走 core 的成长任务队列（扫描全部待办 → 执行 → 轮询进度）。两者不互相
 * 替代；国际版没有成长体系，但面板由管理员手动触发，不按 realm 隐藏。
 */
export function TaskCenterPanel() {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [scan, setScan] = useState<PanelScanItem[] | null>(null);
  const [pending, setPending] = useState<number | null>(null);
  const [queue, setQueue] = useState<PanelQueueStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<number | null>(null);

  const stop = useCallback(() => {
    if (timer.current != null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  }, []);
  useEffect(() => stop, [stop]);

  const poll = useCallback(async () => {
    try {
      const q = await panelApi.queue();
      setQueue(q);
      if (!q.running) stop();
    } catch {
      stop();
    }
  }, [stop]);

  if (!isAdmin) return null;

  async function doScan() {
    setBusy(true);
    try {
      const r = await panelApi.scanAll();
      setScan(r.accounts || []);
      setPending(r.pending_count ?? 0);
      notify.ok(t('taskCenter.scanDone', {n: r.pending_count ?? 0}));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function doRun() {
    setBusy(true);
    try {
      const r = await panelApi.runQueue(1);
      if (!r.started) {
        notify.info(t('taskCenter.queueBusy'), r.message);
        return;
      }
      notify.ok(t('taskCenter.queueStarted', {n: r.total ?? 0}));
      stop();
      await poll();
      timer.current = window.setInterval(() => void poll(), 3000);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  const done = (queue?.items || []).filter((x) => {
    const status = String((x as {status?: string}).status || '');
    return /^(ok|done|skipped|failed)$/.test(status);
  }).length;

  return (
    <section className="rounded-[20px] bg-muted p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-medium">
            <ListChecks className="h-4 w-4" />
            {t('taskCenter.title')}
          </div>
          <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
            {t('taskCenter.desc')}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={() => void doScan()} disabled={busy}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            {t('taskCenter.scan')}
          </Button>
          <Button size="sm" onClick={() => void doRun()} disabled={busy || queue?.running}>
            {queue?.running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            {t('taskCenter.run')}
          </Button>
        </div>
      </div>

      {pending != null && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
          <Badge variant="secondary">{t('taskCenter.pending', {n: pending})}</Badge>
          {(scan || []).filter((x) => (x.growth || []).length > 0).map((x) => (
            <span key={x.uid} className="rounded-md bg-background px-2 py-1">
              {x.nickname || x.uid}: {(x.growth || []).length}
            </span>
          ))}
        </div>
      )}

      {queue && (
        <div className="mt-3 rounded-xl bg-background px-3 py-2 text-[11px]">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{t('taskCenter.queueTitle')}</span>
            <Badge variant={queue.running ? 'default' : 'secondary'}>
              {queue.running ? t('taskCenter.running') : t('taskCenter.queueDone')}
            </Badge>
            <span className="text-muted-foreground">
              {t('taskCenter.progress', {done, total: queue.total})}
            </span>
            {queue.started_at && (
              <span className="text-muted-foreground">{queue.started_at}</span>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
