'use client';

import {useEffect, useState} from 'react';
import {Plus, RotateCcw, Trash2} from 'lucide-react';

import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {settingsApi, errText} from '@/lib/api';
import {notify} from '@/lib/toast';

const DEFAULTS: Record<string, string> = {
  'Claude Code': 'WorkBuddy',
  'Claude': 'WorkBuddy',
  'DeepSeek Harness': 'WorkBuddy',
  'Codex': 'WorkBuddy',
  'ZCode': 'WorkBuddy',
  'workspace root': 'workspace base',
  'are paths the user': 'are paths that the user',
};

type Row = {key: string; value: string};

function rowsFrom(map: Record<string, string>): Row[] {
  return Object.entries(map).map(([key, value]) => ({key, value}));
}

function mapFrom(rows: Row[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    if (row.key.trim()) out[row.key.trim()] = row.value;
  }
  return out;
}

/**
 * identity 出站身份替换编辑器。
 *
 * 只改 system/assistant/developer 文本块；代码块、工具参数、用户正文由 core 跳过。
 * 默认关闭（零回归）；默认映射含 workspace root / are paths the user 两条渠道兼容短语。
 */
export function IdentityPanel() {
  const [enabled, setEnabled] = useState(false);
  const [target, setTarget] = useState('WorkBuddy');
  const [rows, setRows] = useState<Row[]>(rowsFrom(DEFAULTS));
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    settingsApi.upstream().then((cfg) => {
      if (!alive) return;
      const ident = (cfg?.identity || {}) as {
        enabled?: boolean;
        target?: string;
        replacements?: Record<string, string>;
      };
      setEnabled(Boolean(ident.enabled));
      setTarget(ident.target || 'WorkBuddy');
      setRows(rowsFrom({...DEFAULTS, ...(ident.replacements || {})}));
      setLoaded(true);
    }).catch(() => setLoaded(true));
    return () => { alive = false; };
  }, []);

  async function save() {
    setBusy(true);
    try {
      await settingsApi.saveUpstream({
        identity: {enabled, target: target.trim() || 'WorkBuddy', replacements: mapFrom(rows)},
      });
      notify.ok('身份替换已保存');
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    setTarget('WorkBuddy');
    setRows(rowsFrom(DEFAULTS));
  }

  if (!loaded) return null;

  return (
    <div className="rounded-[20px] border border-border/60 bg-card p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-sm font-medium">出站身份替换</div>
          <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
            只替换 system / assistant / developer 文本；代码块、工具参数、用户正文不受影响。
            默认映射含 <code>workspace root → workspace base</code>、
            <code>are paths the user → are paths that the user</code> 两条渠道兼容短语。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Label htmlFor="identity-enabled" className="text-xs text-muted-foreground">启用</Label>
          <Switch id="identity-enabled" checked={enabled} onCheckedChange={setEnabled} />
        </div>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-[160px_1fr] sm:items-center">
        <Label htmlFor="identity-target" className="text-xs text-muted-foreground">目标名称</Label>
        <Input id="identity-target" value={target} onChange={(e) => setTarget(e.target.value)}
               placeholder="WorkBuddy" className="h-8" />
      </div>

      <div className="mt-4 space-y-2">
        <div className="flex items-center justify-between">
          <div className="text-xs font-medium">替换规则</div>
          <div className="flex gap-2">
            <Button type="button" variant="outline" size="sm" className="h-7 px-2 text-xs" onClick={reset}>
              <RotateCcw className="mr-1 h-3.5 w-3.5" />恢复默认
            </Button>
            <Button type="button" variant="outline" size="sm" className="h-7 px-2 text-xs"
                    onClick={() => setRows((r) => [...r, {key: '', value: ''}])}>
              <Plus className="mr-1 h-3.5 w-3.5" />添加
            </Button>
          </div>
        </div>
        {rows.map((row, i) => (
          <div key={i} className="grid grid-cols-[1fr_1fr_auto] gap-2">
            <Input value={row.key} placeholder="原文（如 Claude Code）" className="h-8 font-mono text-xs"
                   onChange={(e) => setRows((r) => r.map((x, j) => j === i ? {...x, key: e.target.value} : x))} />
            <Input value={row.value} placeholder="替换为" className="h-8 font-mono text-xs"
                   onChange={(e) => setRows((r) => r.map((x, j) => j === i ? {...x, value: e.target.value} : x))} />
            <Button type="button" variant="ghost" size="icon" className="h-8 w-8 text-muted-foreground"
                    onClick={() => setRows((r) => r.filter((_, j) => j !== i))}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        ))}
      </div>

      <div className="mt-4 flex justify-end">
        <Button size="sm" disabled={busy} onClick={save}>{busy ? '保存中…' : '保存身份替换'}</Button>
      </div>
    </div>
  );
}
