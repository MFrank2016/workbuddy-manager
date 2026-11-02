'use client';

/**
 * 请求事件日志（Wave 3 · 事件大屏的 manager 版）。
 *
 * 数据源是 core 面板的 `GET /panel/api/request_logs`（逐请求归档，含
 * request_id / 账号 / 模型 / 状态 / outcome / 耗时 / TTFB / tokens / credit /
 * 缓存 / 客户端 IP）。只读页面，每 15 秒增量刷新一次。
 *
 * 与 `/logs` 的区别：那一页读的是 manager 自己的请求日志库（按 key 计费口径），
 * 这里读的是**网关视角**的逐请求事件，排障时能直接看到上游返回了什么。
 */
import {useCallback, useEffect, useMemo, useState} from 'react';
import {Activity, RefreshCw} from 'lucide-react';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {Button} from '@/components/ui/button';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {panelApi, errText} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {fmtDateTime, fmtNumber, fmtCredit} from '@/lib/format';

type EventRow = {
  time: string;
  request_id?: string;
  account?: string;
  model?: string;
  status: number;
  outcome?: string;
  duration_ms?: number;
  ttfb_ms?: number;
  attempts?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  credit?: number;
  credit_known?: boolean;
  cache_hit_tokens?: number;
  cache_miss_tokens?: number;
  client_ip?: string;
};

const OUTCOMES = ['all', 'success', 'http_error', 'stream_error', 'interrupted'] as const;

export default function EventsPage() {
  const t = useT();
  const [rows, setRows] = useState<EventRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [outcome, setOutcome] = useState<(typeof OUTCOMES)[number]>('all');
  const [query, setQuery] = useState('');

  const load = useCallback(async () => {
    try {
      const r = await panelApi.requestLogs(300);
      setRows(((r.entries as EventRow[]) || []).slice().reverse());
      setError(null);
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useHeartbeat(load, 15000);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((r) => {
      if (outcome !== 'all' && (r.outcome || '') !== outcome) return false;
      if (!q) return true;
      return [r.account, r.model, r.request_id, r.client_ip]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(q));
    });
  }, [rows, outcome, query]);

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('events.title')}
        description={t('events.desc')}
        actions={
          <Button size="sm" variant="outline" className="rounded-full" onClick={() => void load()}>
            <RefreshCw className={loading ? 'animate-spin' : ''} />
            {t('events.refresh')}
          </Button>
        }
      />

      <div className="flex flex-wrap items-center gap-2">
        <select
          className="h-8 rounded-full border border-border bg-background px-3 text-xs"
          value={outcome}
          onChange={(e) => setOutcome(e.target.value as (typeof OUTCOMES)[number])}
        >
          {OUTCOMES.map((o) => (
            <option key={o} value={o}>
              {o === 'all' ? t('events.allOutcomes') : o}
            </option>
          ))}
        </select>
        <input
          className="h-8 w-64 rounded-full border border-border bg-background px-3 text-xs"
          placeholder={t('events.searchPlaceholder')}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <span className="text-xs text-muted-foreground">{t('events.count', {n: visible.length})}</span>
      </div>

      {error ? (
        <div className="rounded-[20px] bg-muted p-4 text-xs text-muted-foreground">{error}</div>
      ) : (
        <section className="overflow-x-auto rounded-[20px] bg-muted p-2">
          <table className="w-full min-w-[1100px] border-collapse text-xs">
            <thead>
              <tr className="text-left text-[11px] text-muted-foreground">
                <th className="px-2 py-2">{t('logs.colTime')}</th>
                <th className="px-2 py-2">{t('events.requestId')}</th>
                <th className="px-2 py-2">{t('logs.colAccount')}</th>
                <th className="px-2 py-2">{t('logs.rowModel')}</th>
                <th className="px-2 py-2">{t('logs.rowStatus')}</th>
                <th className="px-2 py-2">outcome</th>
                <th className="px-2 py-2">{t('logs.colLatency')}</th>
                <th className="px-2 py-2">{t('logs.colFirstToken')}</th>
                <th className="px-2 py-2">{t('events.tokens')}</th>
                <th className="px-2 py-2">{t('logs.rowCredit')}</th>
                <th className="px-2 py-2">{t('events.cache')}</th>
                <th className="px-2 py-2">{t('logs.rowIp')}</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((r, i) => (
                <tr key={r.request_id || `${r.time}-${i}`} className="border-t border-border/40">
                  <td className="whitespace-nowrap px-2 py-1.5 tabular-nums">{r.time ? new Date(r.time).toLocaleString() : '—'}</td>
                  <td className="max-w-[140px] truncate px-2 py-1.5 font-mono text-[10.5px]">{r.request_id || '—'}</td>
                  <td className="max-w-[160px] truncate px-2 py-1.5">{r.account || '—'}</td>
                  <td className="max-w-[180px] truncate px-2 py-1.5">{r.model || '—'}</td>
                  <td className="px-2 py-1.5 tabular-nums">{r.status || '—'}</td>
                  <td className="px-2 py-1.5">{r.outcome || '—'}</td>
                  <td className="px-2 py-1.5 tabular-nums">{r.duration_ms ? `${r.duration_ms}ms` : '—'}</td>
                  <td className="px-2 py-1.5 tabular-nums">{r.ttfb_ms ? `${r.ttfb_ms}ms` : '—'}</td>
                  <td className="px-2 py-1.5 tabular-nums">
                    {fmtNumber(r.prompt_tokens)} / {fmtNumber(r.completion_tokens)}
                  </td>
                  <td className="px-2 py-1.5 tabular-nums">{r.credit_known ? fmtCredit(r.credit) : '—'}</td>
                  <td className="px-2 py-1.5 tabular-nums">
                    {r.cache_hit_tokens || r.cache_miss_tokens
                      ? `${fmtNumber(r.cache_hit_tokens)} / ${fmtNumber(r.cache_miss_tokens)}`
                      : '—'}
                  </td>
                  <td className="max-w-[140px] truncate px-2 py-1.5">{r.client_ip || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {visible.length === 0 && (
            <div className="grid h-24 place-items-center text-xs text-muted-foreground">
              {loading ? '…' : t('events.empty')}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
