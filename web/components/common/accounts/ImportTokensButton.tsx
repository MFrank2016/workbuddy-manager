'use client';

/**
 * 批量 refresh token 导入（账号页）。
 *
 * 与「导入/导出账号」的分工：那个吃**文件**（完整 auth JSON，accessToken 直接
 * 落盘）；这个吃**refreshToken 文本**——后端每条都当场找腾讯换一次，落盘的一定
 * 是刚签发的凭据。用户手里往往只有一张 `uid,refresh_token` 的表，没有可上传的
 * 文件，所以这个入口必须能直接粘贴。
 *
 * 三条硬约束（都在 `lib/import-tokens.ts` 里被单测钉住）：
 *   1. 非法 JSON / 超 200 条 / 重复 uid **本地就拦下**，不发请求；
 *   2. 逐条结果按五类分别展示（added / updated / skipped / rejected / failed），
 *      不能把 skipped 混进失败里——那是用户自己没开覆盖的结果，不是错误；
 *   3. 令牌只在提交那一刻经过内存：**不进** localStorage / sessionStorage /
 *      cookie / URL，也不 console.log。提交成功后立刻清空文本域，屏幕上不留原文。
 */
import {useEffect, useMemo, useState} from 'react';
import {
  Ban,
  CircleCheckBig,
  CircleX,
  FileUp,
  KeyRound,
  Loader2,
  MinusCircle,
  ShieldAlert,
  TriangleAlert,
} from 'lucide-react';

import {accountApi, errText} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {
  TokenImportResponse,
  TokenImportStatus,
  UpstreamEndpoint,
} from '@/lib/types';
import {
  MAX_ENTRIES,
  buildImportPayload,
  canSubmit,
  parseImportText,
  type ImportRealm,
} from '@/lib/import-tokens';
import {Button} from '@/components/ui/button';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {Textarea} from '@/components/ui/textarea';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

/** 默认分组在 Select 里的值（与 MoveAccountDialog 同一个约定：不用 0 表示默认） */
const DEFAULT_UPSTREAM = '__default__';

/** 五类结果的视觉：颜色是**语义**（成功绿 / 覆盖蓝 / 跳过灰 / 拒绝琥珀 / 失败红） */
const STATUS_META: Record<
  TokenImportStatus,
  {Icon: typeof CircleCheckBig; className: string; labelKey: string}
> = {
  added: {
    Icon: CircleCheckBig,
    className: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
    labelKey: 'accounts.importTokensStatusAdded',
  },
  updated: {
    Icon: KeyRound,
    className: 'bg-sky-500/10 text-sky-600 dark:text-sky-400',
    labelKey: 'accounts.importTokensStatusUpdated',
  },
  skipped: {
    Icon: MinusCircle,
    className: 'bg-muted text-muted-foreground',
    labelKey: 'accounts.importTokensStatusSkipped',
  },
  rejected: {
    Icon: Ban,
    className: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
    labelKey: 'accounts.importTokensStatusRejected',
  },
  failed: {
    Icon: CircleX,
    className: 'bg-red-500/10 text-red-600 dark:text-red-400',
    labelKey: 'accounts.importTokensStatusFailed',
  },
};

const STATUS_ORDER: TokenImportStatus[] = ['added', 'updated', 'skipped', 'rejected', 'failed'];

/**
 * 后端的固定短码 → i18n 键。
 *
 * 后端刻意只给短码（异常文本可能带 URL / 凭据），所以译文只能由这里映射；
 * 未收录的码回落到后端 `message`（中文原文）——比显示一个裸短码强。
 */
const REASON_KEYS: Record<string, string> = {
  invalid_entry: 'accounts.importTokensReasonInvalidEntry',
  missing_uid: 'accounts.importTokensReasonMissingUid',
  invalid_uid: 'accounts.importTokensReasonInvalidUid',
  missing_refresh_token: 'accounts.importTokensReasonMissingRefresh',
  exists: 'accounts.importTokensReasonExists',
  timeout: 'accounts.importTokensReasonTimeout',
  refresh_failed: 'accounts.importTokensReasonRefreshFailed',
  write_failed: 'accounts.importTokensReasonWriteFailed',
};

export function ImportTokensButton({
  upstreamId,
  groups,
  onSuccess,
}: {
  /** 当前所在分组：打开时作为默认选择（用户多半就是往这一组加号） */
  upstreamId?: number | null;
  groups: UpstreamEndpoint[];
  onSuccess?: () => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [realm, setRealm] = useState<ImportRealm>('');
  const [target, setTarget] = useState<string>(DEFAULT_UPSTREAM);
  const [overwrite, setOverwrite] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<TokenImportResponse | null>(null);
  /** 403 的说明文案（空串 = 没有 403）。存**后端原文**而不是布尔：那是用户唯一能照着做的一句话。 */
  const [forbidden, setForbidden] = useState('');

  // 每次打开都回到「干净」状态：上一次的结果与文本留在界面上，会让人以为
  // 这次也导入了那些号（尤其文本域里还残留 token 时更危险）。
  useEffect(() => {
    if (!open) return;
    setText('');
    setRealm('');
    setTarget(upstreamId == null ? DEFAULT_UPSTREAM : String(upstreamId));
    setOverwrite(false);
    setBusy(false);
    setResult(null);
    setForbidden('');
  }, [open, upstreamId]);

  const parsed = useMemo(() => parseImportText(text), [text]);
  const submitDisabled = busy || !canSubmit(parsed);

  // 只列有本地账号目录的分组：没有目录的组后端会 400 `upstream_auth_dir_unset`，
  // 让它出现在下拉里等于给一个「点了必失败」的选项。
  const groupOptions = groups.filter(
    (group) => !group.is_default && group.id != null && Boolean(group.auth_dir));

  async function submit() {
    if (submitDisabled) return;
    setBusy(true);
    setForbidden('');
    try {
      const body = buildImportPayload(parsed, {
        realm,
        upstreamId: target === DEFAULT_UPSTREAM ? null : Number(target),
        overwrite,
      });
      const response = await accountApi.importTokens(body);
      setResult(response);
      // 提交成功即清空文本域：令牌不该在屏幕上多留一秒（更不该被误当草稿留存）。
      setText('');
      const {counts} = response;
      if (counts.added || counts.updated) {
        notify.ok(
          t('accounts.importTokensDone'),
          t('accounts.importTokensDoneDetail', {
            added: counts.added,
            updated: counts.updated,
            skipped: counts.skipped,
            rejected: counts.rejected,
            failed: counts.failed,
          }),
        );
      } else {
        notify.info(
          t('accounts.importTokensNoChange'),
          t('accounts.importTokensDoneDetail', {
            added: counts.added,
            updated: counts.updated,
            skipped: counts.skipped,
            rejected: counts.rejected,
            failed: counts.failed,
          }),
        );
      }
      onSuccess?.();
    } catch (error) {
      const status = (error as {response?: {status?: number}}).response?.status;
      if (status === 403) {
        // 403 在这里有两个来源（角色不够 / 该接口只收会话），后端原文才是
        // 用户能照着做的那句话——单独一块常驻提示，而不是一闪而过的 toast。
        // `errText` 会先过短语表再兜底，所以非中文界面也能读到对应译文。
        setForbidden(errText(error));
        return;
      }
      notify.err(errText(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        size="sm"
        variant="outline"
        className="rounded-full"
        onClick={() => setOpen(true)}
        title={t('accounts.importTokens')}
      >
        <FileUp />
        <span>{t('accounts.importTokens')}</span>
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-[560px]" showCloseButton>
          <DialogHeader>
            <DialogTitle>{t('accounts.importTokensTitle')}</DialogTitle>
            <DialogDescription>{t('accounts.importTokensDesc')}</DialogDescription>
          </DialogHeader>

          <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
            <div className="space-y-4 px-6 pb-2">
              {/* 三个落点参数：这一批号加进哪个版本、哪个分组、要不要覆盖 */}
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">
                    {t('accounts.importTokensRealm')}
                  </Label>
                  <Select
                    value={realm === '' ? 'auto' : realm}
                    onValueChange={(value) => setRealm(value === 'auto' ? '' : (value as ImportRealm))}
                    disabled={busy}
                  >
                    <SelectTrigger aria-label={t('accounts.importTokensRealm')}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="auto">{t('accounts.importTokensRealmAuto')}</SelectItem>
                      <SelectItem value="cn">{t('realm.cn')}</SelectItem>
                      <SelectItem value="global">{t('realm.global')}</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">
                    {t('accounts.importTokensUpstream')}
                  </Label>
                  <Select value={target} onValueChange={setTarget} disabled={busy}>
                    <SelectTrigger aria-label={t('accounts.importTokensUpstream')}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={DEFAULT_UPSTREAM}>
                        {t('accounts.importTokensUpstreamDefault')}
                      </SelectItem>
                      {groupOptions.map((group) => (
                        <SelectItem key={group.id} value={String(group.id)}>
                          {group.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <label className="flex items-center justify-between gap-3 rounded-xl bg-muted/50 px-3 py-2">
                <span className="min-w-0">
                  <span className="block text-[13px] font-medium">
                    {t('accounts.importTokensOverwrite')}
                  </span>
                  <span className="mt-0.5 block text-[11px] leading-4 text-muted-foreground">
                    {t('accounts.importTokensOverwriteHint')}
                  </span>
                </span>
                <Switch
                  checked={overwrite}
                  onCheckedChange={setOverwrite}
                  disabled={busy}
                  aria-label={t('accounts.importTokensOverwrite')}
                />
              </label>

              <div className="space-y-1.5">
                <div className="flex items-center justify-between gap-2">
                  <Label className="text-[11px] text-muted-foreground" htmlFor="import-tokens-input">
                    {t('accounts.importTokensInput')}
                  </Label>
                  {parsed.count > 0 && (
                    <span className="text-[11px] tabular-nums text-muted-foreground">
                      {t('accounts.importTokensCount', {n: parsed.count, max: MAX_ENTRIES})}
                    </span>
                  )}
                </div>
                <Textarea
                  id="import-tokens-input"
                  className="min-h-32 bg-background font-mono text-xs"
                  value={text}
                  onChange={(event) => setText(event.target.value)}
                  placeholder={t('accounts.importTokensPlaceholder')}
                  disabled={busy}
                  spellCheck={false}
                  autoComplete="off"
                  aria-invalid={
                    parsed.fatal === 'invalid_json' || parsed.fatal === 'too_many'
                    || parsed.duplicates.length > 0 || parsed.errors.length > 0
                  }
                />
                <p className="text-[11px] leading-4 text-muted-foreground">
                  {t('accounts.importTokensHint', {max: MAX_ENTRIES})}
                </p>
                <p className="flex items-start gap-1.5 text-[11px] leading-4 text-muted-foreground">
                  <ShieldAlert className="mt-px size-3.5 shrink-0 text-muted-foreground/80" />
                  {t('accounts.importTokensPrivacy')}
                </p>
              </div>

              {/* 本地校验：必须在**提交前**说出来，而不是等后端 400 */}
              {parsed.fatal === 'invalid_json' && (
                <ErrorNote text={t('accounts.importTokensInvalidJson')} />
              )}
              {parsed.fatal === 'too_many' && (
                <ErrorNote text={t('accounts.importTokensTooMany', {max: MAX_ENTRIES, n: parsed.count})} />
              )}
              {parsed.duplicates.length > 0 && (
                <ErrorNote
                  text={t('accounts.importTokensDuplicates', {uids: parsed.duplicates.join(', ')})}
                />
              )}
              {parsed.errors.length > 0 && (
                <ErrorNote
                  text={t('accounts.importTokensRowErrors', {n: parsed.errors.length})}
                  rows={parsed.errors.slice(0, 5).map((item) => ({
                    uid: item.uid,
                    reason: t(REASON_KEYS[item.reason] ?? 'accounts.importTokensReasonInvalidEntry'),
                  }))}
                  more={Math.max(0, parsed.errors.length - 5)}
                />
              )}

              {/* 403：后端原文是唯一能照着做的一句话，单独常驻显示 */}
              {forbidden !== '' && (
                <div className="flex items-start gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-[12px] leading-5 text-amber-700 dark:text-amber-300">
                  <ShieldAlert className="mt-0.5 size-4 shrink-0" />
                  <div className="min-w-0">
                    <div className="font-medium">{t('accounts.importTokensForbiddenTitle')}</div>
                    {/* 后端原文优先（它说清了下一步做什么）；拿不到才用通用说明 */}
                    <div className="mt-0.5">
                      {forbidden || t('accounts.importTokensForbiddenBody')}
                    </div>
                  </div>
                </div>
              )}

              {result && <ImportResults result={result} />}
            </div>
          </DialogBody>

          <DialogFooter>
            <Button variant="outline" className="rounded-full" onClick={() => setOpen(false)}>
              {t('common.close')}
            </Button>
            <Button className="rounded-full" onClick={submit} disabled={submitDisabled}>
              {busy && <Loader2 className="animate-spin" />}
              {busy ? t('accounts.importTokensSubmitting') : t('accounts.importTokensSubmit')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function ErrorNote({
  text,
  rows,
  more,
}: {
  text: string;
  rows?: Array<{uid: string; reason: string}>;
  more?: number;
}) {
  const t = useT();
  return (
    <div className="flex items-start gap-2 rounded-xl bg-amber-500/10 px-3 py-2">
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
      <div className="min-w-0 flex-1">
        <div className="text-[12px] leading-5 text-amber-700 dark:text-amber-300">{text}</div>
        {rows && rows.length > 0 && (
          <ul className="mt-1 space-y-0.5">
            {rows.map((row, index) => (
              <li
                key={`${row.uid}-${index}`}
                className="truncate font-mono text-[11px] text-amber-700/90 dark:text-amber-300/90"
              >
                {row.uid || t('accounts.importTokensUnnamedRow')} — {row.reason}
              </li>
            ))}
            {more ? (
              <li className="text-[11px] text-amber-700/80 dark:text-amber-300/80">
                {t('accounts.importTokensMoreRows', {n: more})}
              </li>
            ) : null}
          </ul>
        )}
      </div>
    </div>
  );
}

/** 逐条结果：先给五类计数（一眼看全），再按原序列出每一条与原因。 */
function ImportResults({result}: {result: TokenImportResponse}) {
  const t = useT();
  return (
    <div className="space-y-2 rounded-xl border border-border/70 p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-[12px] font-medium">{t('accounts.importTokensResultTitle')}</span>
        {STATUS_ORDER.map((status) => {
          const meta = STATUS_META[status];
          return (
            <span
              key={status}
              className={
                'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium tabular-nums '
                + meta.className
              }
            >
              <meta.Icon className="size-3" />
              {t(meta.labelKey)}
              <span>{result.counts[status] ?? 0}</span>
            </span>
          );
        })}
      </div>

      <ul className="max-h-56 space-y-1 overflow-y-auto pr-1">
        {result.results.map((item, index) => {
          const meta = STATUS_META[item.status] ?? STATUS_META.failed;
          const reasonKey = item.reason ? REASON_KEYS[item.reason] : undefined;
          const reason = reasonKey ? t(reasonKey) : item.message;
          return (
            <li
              key={`${item.uid}-${index}`}
              className="flex items-start gap-2 rounded-lg bg-muted/45 px-2 py-1.5"
            >
              <span className={
                'mt-px inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-[10.5px] font-medium '
                + meta.className
              }>
                <meta.Icon className="size-3" />
                {t(meta.labelKey)}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-mono text-[11px]">
                  {item.uid || t('accounts.importTokensUnnamedRow')}
                </span>
                {reason ? (
                  <span className="block text-[11px] leading-4 text-muted-foreground">{reason}</span>
                ) : null}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
