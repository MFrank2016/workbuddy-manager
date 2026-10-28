'use client';

import {useMemo, useState} from 'react';
import {Loader2, ScanSearch, TriangleAlert} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {useAsyncAll} from '@/lib/use-async-data';
import {accountApi, errText, modelApi} from '@/lib/api';
import {useI18n} from '@/lib/i18n/provider';
import type {Realm} from '@/lib/realm-context';
import type {
  Account,
  ProbeModelResult,
  ProbeModelsResponse,
  ProbeStatus,
} from '@/lib/types';

/** 单次最多 60 条组合（与后端一致）：每一条都是一次真实对话请求。 */
const MAX_COMBOS = 60;
const MAX_ACCOUNTS = 20;
const MAX_MODELS = 10;

const STATUS_KEY: Record<ProbeStatus, string> = {
  ok: 'accounts.probeStatusOk',
  rate: 'accounts.probeStatusRate',
  credit: 'accounts.probeStatusCredit',
  auth: 'accounts.probeStatusAuth',
  no_model: 'accounts.probeStatusNoModel',
  error: 'accounts.probeStatusError',
};

const STATUS_STYLE: Record<ProbeStatus, string> = {
  ok: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  rate: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  credit: 'bg-orange-500/10 text-orange-600 dark:text-orange-400',
  auth: 'bg-red-500/10 text-red-600 dark:text-red-400',
  no_model: 'bg-muted text-muted-foreground',
  error: 'bg-red-500/10 text-red-600 dark:text-red-400',
};

const STATUS_ORDER: ProbeStatus[] = ['ok', 'rate', 'credit', 'auth', 'no_model', 'error'];

/**
 * 账号 × 模型主动探测（issue #270）。
 *
 * 解决的问题：面板此前只能**被动**知道「某号某模型被限流」——要等上游真的拒绝
 * 过一次。新号接入、上游新增模型、遇到 `503 no_healthy_account` 时，都无法回答
 * 「这个号到底能不能用这个模型」。
 *
 * 探测会发**真实请求**（max_tokens=1 的最小对话），所以：
 *   · 账号内串行、每条间隔 600ms，账号间最多 4 路并发（后端实现）；
 *   · 上限 60 条组合，界面先显示「本次将发起 N 次请求」；
 *   · `11102 无此模型` 不算错误——账号本身没问题。
 */
export function ModelProbeDialog({
  open,
  onOpenChange,
  accounts,
  upstreamId,
  realm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 当前版本的账号（父组件已按 realm 过滤） */
  accounts: Account[];
  upstreamId: number | null;
  realm: Realm;
}) {
  const {t} = useI18n();
  const [pickedAccounts, setPickedAccounts] = useState<string[]>(
    () => accounts.slice(0, MAX_ACCOUNTS).map((a) => a.file),
  );
  const [pickedModels, setPickedModels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<ProbeModelsResponse | null>(null);

  const catalog = useAsyncAll({catalog: () => modelApi.catalog(realm)}, [realm]);
  const models = catalog.values.catalog?.models ?? [];
  const modelName = (id: string) => models.find((m) => m.id === id)?.name || id;

  const combos = pickedAccounts.length * pickedModels.length;

  const matrix = useMemo(() => {
    const map = new Map<string, ProbeModelResult>();
    for (const r of result?.results ?? []) {
      map.set(`${r.file}\u0000${r.model}`, r);
    }
    return map;
  }, [result]);

  function toggle(list: string[], value: string, limit: number): string[] {
    if (list.includes(value)) return list.filter((v) => v !== value);
    if (list.length >= limit) return list;
    return [...list, value];
  }

  async function run() {
    setBusy(true);
    setError('');
    setResult(null);
    try {
      const r = await accountApi.probeModels(pickedAccounts, pickedModels, upstreamId);
      setResult(r);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  }

  const canRun = !busy && pickedAccounts.length > 0 && pickedModels.length > 0
    && combos <= MAX_COMBOS;

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!busy) onOpenChange(v); }}>
      <DialogContent className="max-h-[88vh] max-w-[760px] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ScanSearch className="size-4" />
            {t('accounts.probeTitle')}
          </DialogTitle>
          <DialogDescription className="pt-1 text-[12px] leading-relaxed">
            {t('accounts.probeDesc')}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 sm:grid-cols-2">
          {/* 账号 */}
          <div>
            <div className="mb-1 flex items-center justify-between text-xs font-medium">
              <span>{t('accounts.probeAccounts')}</span>
              <button
                type="button"
                className="text-[11px] text-primary hover:underline"
                onClick={() => setPickedAccounts(
                  pickedAccounts.length ? [] : accounts.slice(0, MAX_ACCOUNTS).map((a) => a.file),
                )}
              >
                {pickedAccounts.length ? t('accounts.probeClear') : t('accounts.probeSelectAll')}
              </button>
            </div>
            <div className="max-h-[150px] overflow-y-auto rounded-xl border p-2">
              {accounts.length === 0 && (
                <div className="p-2 text-[11px] text-muted-foreground">
                  {t('dashboard.noAccounts')}
                </div>
              )}
              {accounts.map((a) => (
                <label key={a.file}
                       className="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-[12px] hover:bg-muted">
                  <input
                    type="checkbox"
                    checked={pickedAccounts.includes(a.file)}
                    onChange={() => setPickedAccounts((v) => toggle(v, a.file, MAX_ACCOUNTS))}
                  />
                  <span className="truncate">{a.nickname || a.uid || a.file}</span>
                </label>
              ))}
            </div>
          </div>

          {/* 模型 */}
          <div>
            <div className="mb-1 flex items-center justify-between text-xs font-medium">
              <span>{t('accounts.probeModels')}</span>
              <button
                type="button"
                className="text-[11px] text-primary hover:underline"
                onClick={() => setPickedModels(
                  pickedModels.length ? [] : models.slice(0, MAX_MODELS).map((m) => m.id),
                )}
              >
                {pickedModels.length ? t('accounts.probeClear') : t('accounts.probeSelectAll')}
              </button>
            </div>
            <div className="max-h-[150px] overflow-y-auto rounded-xl border p-2">
              {models.length === 0 && (
                <div className="p-2 text-[11px] text-muted-foreground">
                  {t('accounts.probeNoModels')}
                </div>
              )}
              {models.map((m) => (
                <label key={m.id}
                       className="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-[12px] hover:bg-muted">
                  <input
                    type="checkbox"
                    checked={pickedModels.includes(m.id)}
                    onChange={() => setPickedModels((v) => toggle(v, m.id, MAX_MODELS))}
                  />
                  <span className="truncate" title={m.id}>{m.name || m.id}</span>
                </label>
              ))}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          <span>{t('accounts.probeRequests', {n: combos})}</span>
          {combos > MAX_COMBOS && (
            <span className="text-amber-600">
              {t('accounts.probeTooMany', {n: combos})}
            </span>
          )}
        </div>

        {error && (
          <div className="flex items-start gap-2 rounded-xl bg-red-500/10 px-3 py-2 text-[11px] text-red-600">
            <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {result && (
          <div className="space-y-2">
            <div className="flex flex-wrap gap-1.5">
              {STATUS_ORDER.filter((s) => (result.summary[s] ?? 0) > 0).map((s) => (
                <span key={s}
                      className={`rounded-full px-2 py-0.5 text-[10px] ${STATUS_STYLE[s]}`}>
                  {t(STATUS_KEY[s])} {result.summary[s]}
                </span>
              ))}
            </div>
            <div className="overflow-x-auto rounded-xl border">
              <table className="w-full border-collapse text-[11px]">
                <thead>
                  <tr className="border-b bg-muted/50">
                    <th className="whitespace-nowrap px-2 py-1.5 text-left font-medium">
                      {t('accounts.probeAccounts')}
                    </th>
                    {pickedModels.map((m) => (
                      <th key={m} className="whitespace-nowrap px-2 py-1.5 text-center font-medium"
                          title={m}>
                        {modelName(m)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {accounts.filter((a) => pickedAccounts.includes(a.file)).map((a) => (
                    <tr key={a.file} className="border-b last:border-0">
                      <td className="max-w-[160px] truncate px-2 py-1.5" title={a.file}>
                        {a.nickname || a.uid || a.file}
                      </td>
                      {pickedModels.map((m) => {
                        const r = matrix.get(`${a.file}\u0000${m}`);
                        return (
                          <td key={m} className="px-1.5 py-1 text-center">
                            {r ? (
                              <span
                                className={`inline-block rounded-full px-2 py-0.5 ${STATUS_STYLE[r.status]}`}
                                title={`${t(STATUS_KEY[r.status])}\n${r.message}`}
                              >
                                {t(STATUS_KEY[r.status])}
                              </span>
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <DialogFooter>
          <Button variant="ghost" size="sm" className="rounded-full"
                  disabled={busy} onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button size="sm" className="rounded-full" disabled={!canRun} onClick={run}>
            {busy ? <Loader2 className="mr-1.5 size-3.5 animate-spin" /> : null}
            {busy ? t('accounts.probeRunning') : t('accounts.probeRun')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
