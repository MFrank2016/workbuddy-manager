/**
 * 仪表盘「即将到期积分」用的到期明细。
 *
 * 积分元数据按 uid 存，一次查询会带上所有分组、所有版本的账号。余额已经按
 * 当前版本的账号名单算过了，到期必须走**同一份名单**：否则切到国际版仍会
 * 显示国内版更早的那一笔，金额、时刻和紧急色一起串。刷新换的是同一份 map，
 * 不按名单收的话刷新也改不掉。
 *
 * 只收调用方已经按版本筛过的账号。realm 为空视为国内版的判定留在调用方，
 * 与账号计数共用，不在这里再写一遍。
 *
 * 同一 uid 出现在多个分组时只计一次——元数据本身就是按 uid 一份。
 * 不修改传入的账号列表、元数据，以及其中的到期数组。
 */
import type {Account, CreditExpiry, CreditExpiryBucket, CreditsMeta} from './types';

export function collectScopedExpiries(
  accounts: readonly {uid: string}[],
  meta: Record<string, CreditsMeta>,
): CreditExpiry[] {
  const seen = new Set<string>();
  const out: CreditExpiry[] = [];
  for (const account of accounts) {
    if (seen.has(account.uid)) continue;
    seen.add(account.uid);
    const expiries = meta[account.uid]?.expiries;
    if (!expiries) continue;
    for (const expiry of expiries) out.push(expiry);
  }
  return out.sort((a, b) => a.at - b.at);
}

/** 一个账号的到期明细行：最早批次在前，其余由界面折叠成「随后 …」。 */
export interface AccountExpiryRow {
  uid: string;
  name: string;
  buckets: CreditExpiry[];
  note?: string;
}

/**
 * 把当前版本的账号（`scoped`）转成「每账号一行」的到期明细。
 *
 * 与 `collectScopedExpiries` 的区别：后者把所有账号的批次拍平给汇总卡片；
 * 这里保留账号边界，供明细区每账号一行展示，行与行按各自最早到期升序。
 *
 * 回落规则（core 的 `credits_expiry_buckets` 可能缺失）：没有有效分桶时，
 * 仅当 `credits_earliest_remaining > 0` 且 `credits_earliest_expiry` 的年份
 * 大于 1970 才用它。Go 的 `time.Time` + `omitempty` 仍会输出
 * `0001-01-01T00:00:00Z`，不守卫就会渲染出公元 1 年的假行。
 *
 * 同一 uid 只出一行（元数据按 uid 一份）；不修改传入的账号。
 */
export function collectAccountExpiries(accounts: readonly Account[]): AccountExpiryRow[] {
  const seen = new Set<string>();
  const out: AccountExpiryRow[] = [];
  for (const account of accounts) {
    if (seen.has(account.uid)) continue;
    seen.add(account.uid);
    const buckets = bucketList(account.credits_expiry_buckets);
    if (!buckets.length) {
      const fallback = fallbackBucket(account);
      if (!fallback) continue;
      buckets.push(fallback);
    }
    buckets.sort((a, b) => a.at - b.at);
    const row: AccountExpiryRow = {
      uid: account.uid,
      name: account.nickname || account.uid,
      buckets,
    };
    if (account.note) row.note = account.note;
    out.push(row);
  }
  return out.sort((a, b) => a.buckets[0].at - b.buckets[0].at);
}

/** 只接受 `{date, expire_at, remain}` 形态，丢弃缺字段 / 非正额度 / 非法时刻。 */
function bucketList(raw: CreditExpiryBucket[] | null | undefined): CreditExpiry[] {
  if (!Array.isArray(raw)) return [];
  const out: CreditExpiry[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const at = typeof item.expire_at === 'string' ? Date.parse(item.expire_at) : NaN;
    const amount = item.remain;
    if (!Number.isFinite(at) || at <= 0) continue;
    if (typeof amount !== 'number' || !Number.isFinite(amount) || amount <= 0) continue;
    out.push({at: Math.floor(at / 1000), amount});
  }
  return out;
}

/** 回落单批：零值时间（公元 1 年）与 0 余额都必须排除。 */
function fallbackBucket(account: Account): CreditExpiry | null {
  const remain = account.credits_earliest_remaining;
  if (typeof remain !== 'number' || !Number.isFinite(remain) || remain <= 0) return null;
  const raw = account.credits_earliest_expiry;
  if (typeof raw !== 'string' || !raw) return null;
  const at = Date.parse(raw);
  if (!Number.isFinite(at)) return null;
  if (new Date(at).getUTCFullYear() <= 1970) return null;
  return {at: Math.floor(at / 1000), amount: remain};
}
