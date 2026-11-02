'use client';

/**
 * 余额趋势（Wave 3 · 余额采样折线）。
 *
 * 数据源是 core 面板的 `credit_history`（逐账号积分变动流水，含 before/after）。
 * 聚合口径：**每个账号每天取最后一条 after（当日余额快照），再跨账号求和**。
 * 不按 delta 累加——delta 只覆盖被观测到的变动，签到/发放等未入账的变动会让
 * 累加值偏离真实余额；after 是上游回执里的权威余额。
 */
import {useEffect, useMemo, useState} from 'react';
import {Area, CartesianGrid, ComposedChart, ResponsiveContainer, Tooltip, XAxis, YAxis} from 'recharts';
import {Coins} from 'lucide-react';
import {panelApi, errText} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {fmtCredit} from '@/lib/format';
import type {PanelCreditHistoryEntry} from '@/lib/types';

type Point = {day: string; total: number};

export function BalanceTrend() {
  const t = useT();
  const [entries, setEntries] = useState<PanelCreditHistoryEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    panelApi.creditHistory(null, 1000)
      .then((r) => {
        if (alive) setEntries(r.entries || []);
      })
      .catch((e) => {
        if (alive) setError(errText(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  const data = useMemo<Point[]>(() => {
    if (!entries || entries.length === 0) return [];
    const perDay = new Map<string, Map<string, number>>();
    for (const e of entries) {
      const day = (e.time || '').slice(0, 10);
      if (!day) continue;
      if (!perDay.has(day)) perDay.set(day, new Map());
      perDay.get(day)!.set(e.uid, e.after);
    }
    return [...perDay.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([day, m]) => ({day, total: [...m.values()].reduce((s, v) => s + v, 0)}));
  }, [entries]);

  return (
    <section className="rounded-[20px] bg-muted p-4">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Coins className="h-4 w-4" />
          {t('stats.balanceTrend')}
        </div>
        <div className="text-xs text-muted-foreground">{t('stats.balanceTrendDesc')}</div>
      </div>
      {error ? (
        <div className="grid h-[220px] place-items-center text-xs text-muted-foreground">{error}</div>
      ) : !entries ? (
        <div className="h-[220px] animate-pulse rounded-xl bg-muted-foreground/10" />
      ) : data.length < 2 ? (
        <div className="grid h-[220px] place-items-center text-xs text-muted-foreground">
          {t('stats.balanceTrendEmpty')}
        </div>
      ) : (
        <div className="h-[220px]">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data} margin={{top: 4, right: 8, bottom: 0, left: -8}}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-border" vertical={false} />
              <XAxis dataKey="day" tick={{fontSize: 11}} />
              <YAxis tick={{fontSize: 11}} width={56} />
              <Tooltip formatter={(v: number) => fmtCredit(v)} />
              <Area
                type="monotone"
                dataKey="total"
                stroke="var(--chart-2)"
                fill="var(--chart-2)"
                fillOpacity={0.15}
                strokeWidth={2}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}
