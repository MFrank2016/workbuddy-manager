/**
 * `collectScopedExpiries` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/dashboard-credits.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_dashboard_credit_expiry`
 *
 * 为什么值得单独测：仪表盘的积分余额已经按当前版本过滤，到期明细却曾经扫
 * **全部** uid 的元数据。国内版更早的一笔就会出现在国际版卡片上——金额、
 * 时刻、紧急色一起串，刷新账号也改不掉，因为刷新换的是同一份跨版本的 map。
 */
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {collectAccountExpiries, collectScopedExpiries} from './dashboard-credits.ts';
import {groupExpiriesByDay} from './display-prefs.ts';

let failed = 0;

function check(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    console.log(`  ok  ${name}`);
  } else {
    console.log(`  FAIL ${name}\n       got  ${g}\n       want ${w}`);
    failed += 1;
  }
}

/** 与 dashboard 卡片同一条线：7 天内算紧急。 */
function expiryUrgent(entry, nowSec) {
  return !!entry && entry.at - nowSec < 7 * 86400;
}

/** 本地时区下的「某天 12:00」。 */
function localNoon(y, m, d) {
  return Math.floor(new Date(y, m - 1, d, 12, 0, 0).getTime() / 1000);
}

/** 存量账号 realm 为空视为国内版，与仪表盘 `scopedSlices` 同一判据。 */
function scopedTo(accounts, realm) {
  return accounts.filter((a) => (a.realm ?? 'cn') === realm);
}

const now = 1_800_000_000;
const domesticAt = now + 3600;
const intlAt = now + 30 * 86400;

const pool = [
  {uid: 'cn-1', realm: 'cn'},
  {uid: 'intl-1', realm: 'intl'},
  {uid: 'legacy'},
];
const meta = {
  'cn-1': {expiries: [{at: domesticAt, amount: 596}]},
  'intl-1': {expiries: [{at: intlAt, amount: 250}]},
  legacy: {expiries: [{at: domesticAt + 10, amount: 7}]},
  removed: {expiries: [{at: domesticAt - 100, amount: 1}]},
};

console.log('collectScopedExpiries:');

const intl = collectScopedExpiries(scopedTo(pool, 'intl'), meta);
const domestic = collectScopedExpiries(scopedTo(pool, 'cn'), meta);
check('international keeps its own package', intl, [{at: intlAt, amount: 250}]);
check('domestic keeps its own packages', domestic, [
  {at: domesticAt, amount: 596},
  {at: domesticAt + 10, amount: 7},
]);
check('international urgency ignores the other realm', expiryUrgent(intl[0], now), false);
check('domestic urgency follows its own earliest', expiryUrgent(domestic[0], now), true);

const noon = localNoon(2026, 10, 12);
const dayMeta = {
  a: {expiries: [{at: noon + 3600, amount: 100}, {at: noon, amount: 50}]},
  b: {expiries: [{at: noon + 7200, amount: 100}]},
  other: {expiries: [{at: noon + 1800, amount: 596}]},
};
const dayAccounts = [{uid: 'a'}, {uid: 'b'}];
const dayList = collectScopedExpiries(dayAccounts, dayMeta);
check('same local day sums only the selected packages', groupExpiriesByDay(dayList), [
  {at: noon, amount: 250},
]);

check('empty account list yields nothing', collectScopedExpiries([], meta), []);
check('missing metadata yields nothing', collectScopedExpiries([{uid: 'absent'}], meta), []);
check('omitted expiries yield nothing', collectScopedExpiries([{uid: 'bare'}], {bare: {}}), []);
check('empty expiries yield nothing', collectScopedExpiries([{uid: 'empty'}], {empty: {expiries: []}}), []);
check(
  'metadata for an account outside the list stays out',
  collectScopedExpiries([{uid: 'intl-1'}], meta).some((e) => e.amount === 1),
  false,
);

const dupMeta = {
  shared: {expiries: [{at: 300, amount: 3}, {at: 100, amount: 1}]},
  other: {expiries: [{at: 200, amount: 2}]},
};
const dupAccounts = [{uid: 'other'}, {uid: 'shared'}, {uid: 'shared'}];
check('unique uids, chronological, no double count', collectScopedExpiries(dupAccounts, dupMeta), [
  {at: 100, amount: 1},
  {at: 200, amount: 2},
  {at: 300, amount: 3},
]);

const accountsBefore = JSON.stringify(dupAccounts);
const metaBefore = JSON.stringify(dupMeta);
const expiryOrder = dupMeta.shared.expiries.map((e) => e.at);
collectScopedExpiries(dupAccounts, dupMeta);
check('does not mutate accounts', JSON.stringify(dupAccounts), accountsBefore);
check('does not mutate metadata', JSON.stringify(dupMeta), metaBefore);
check('does not reorder stored expiries', dupMeta.shared.expiries.map((e) => e.at), expiryOrder);

const refreshed = {
  ...meta,
  'intl-1': {expiries: [{at: intlAt + 86400, amount: 80}]},
};
check('refresh replaces the selected package', collectScopedExpiries(scopedTo(pool, 'intl'), refreshed), [
  {at: intlAt + 86400, amount: 80},
]);
const unrelated = {
  ...meta,
  'cn-1': {expiries: [{at: now, amount: 9999}]},
};
check(
  'an unrelated uid cannot change the selected list',
  collectScopedExpiries(scopedTo(pool, 'intl'), unrelated),
  intl,
);

console.log('collectAccountExpiries:');

/** ISO 时刻 → epoch 秒（与 CreditExpiry.at 同单位）。 */
function isoAt(iso) {
  return Math.floor(Date.parse(iso) / 1000);
}

// (i) 2 个账号各带分桶 → 2 行，不能被拍平成 1 行；行按最早到期升序。
const twoAccounts = collectAccountExpiries([
  {
    uid: 'b',
    nickname: 'Beta',
    credits_expiry_buckets: [
      {date: '2030-02-01', expire_at: '2030-02-01T00:00:00Z', remain: 5},
    ],
  },
  {
    uid: 'a',
    nickname: 'Alpha',
    credits_expiry_buckets: [
      {date: '2030-01-01', expire_at: '2030-01-01T00:00:00Z', remain: 9},
    ],
  },
]);
check('case i: two accounts keep two rows', twoAccounts.length, 2);
check('case i: rows sorted by earliest expiry', twoAccounts.map((r) => r.uid), ['a', 'b']);
check('case i: buckets converted to {at, amount}', twoAccounts[0], {
  uid: 'a',
  name: 'Alpha',
  buckets: [{at: isoAt('2030-01-01T00:00:00Z'), amount: 9}],
});

// (ii) 单账号 3 个乱序分桶 → 1 行，批次升序，其余 2 批折叠。
const threeBuckets = collectAccountExpiries([{
  uid: 'solo',
  nickname: 'Solo',
  note: '主力号',
  credits_expiry_buckets: [
    {date: '2030-03-03', expire_at: '2030-03-03T00:00:00Z', remain: 3},
    {date: '2030-01-01', expire_at: '2030-01-01T00:00:00Z', remain: 1},
    {date: '2030-02-02', expire_at: '2030-02-02T00:00:00Z', remain: 2},
  ],
}]);
check('case ii: three buckets collapse to one row', threeBuckets.length, 1);
check('case ii: buckets sorted ascending', threeBuckets[0].buckets.map((b) => b.amount), [1, 2, 3]);
check('case ii: two batches fold behind the first', threeBuckets[0].buckets.length - 1, 2);
check('case ii: note is carried for the row label', threeBuckets[0].note, '主力号');

// (iii) 无分桶但 remaining > 0 → 回落单批单行；非列表/全无效分桶同样回落。
const fallback = collectAccountExpiries([{
  uid: 'fallback',
  nickname: 'Fallback',
  credits_expiry_buckets: [],
  credits_earliest_expiry: '2030-05-01T00:00:00Z',
  credits_earliest_remaining: 42,
}]);
check('case iii: fallback yields one row', fallback, [{
  uid: 'fallback',
  name: 'Fallback',
  buckets: [{at: isoAt('2030-05-01T00:00:00Z'), amount: 42}],
}]);
const malformedFallback = collectAccountExpiries([{
  uid: 'malformed',
  nickname: 'Malformed',
  credits_expiry_buckets: [
    {date: 'x'},
    {expire_at: '2030-01-01T00:00:00Z', remain: 0},
    {expire_at: 'not-a-date', remain: 5},
    null,
  ],
  credits_earliest_expiry: '2030-06-01T00:00:00Z',
  credits_earliest_remaining: 7,
}]);
check('case iii: malformed buckets fall back', malformedFallback, [{
  uid: 'malformed',
  name: 'Malformed',
  buckets: [{at: isoAt('2030-06-01T00:00:00Z'), amount: 7}],
}]);
check('case iii: non-list buckets fall back', collectAccountExpiries([{
  uid: 'stringy',
  nickname: 'Stringy',
  credits_expiry_buckets: 'nope',
  credits_earliest_expiry: '2030-06-01T00:00:00Z',
  credits_earliest_remaining: 3,
}]), [{
  uid: 'stringy',
  name: 'Stringy',
  buckets: [{at: isoAt('2030-06-01T00:00:00Z'), amount: 3}],
}]);

// (iv) 零值时间（公元 1 年）或 remaining == 0 → 不出假行 / 空结果。
const zeroTime = collectAccountExpiries([{
  uid: 'zero-time',
  nickname: 'ZeroTime',
  credits_expiry_buckets: [],
  credits_earliest_expiry: '0001-01-01T00:00:00Z',
  credits_earliest_remaining: 10,
}]);
check('case iv: zero-time earliest yields no row', zeroTime, []);
check(
  'case iv: no year-1 date ever rendered',
  zeroTime.some((r) => r.buckets.some((b) => new Date(b.at * 1000).getUTCFullYear() === 1)),
  false,
);
check('case iv: zero remaining yields no row', collectAccountExpiries([{
  uid: 'zero-remain',
  nickname: 'ZeroRemain',
  credits_earliest_expiry: '2030-01-01T00:00:00Z',
  credits_earliest_remaining: 0,
}]), []);
check('case iv: negative remaining yields no row', collectAccountExpiries([{
  uid: 'negative',
  nickname: 'Negative',
  credits_earliest_expiry: '2030-01-01T00:00:00Z',
  credits_earliest_remaining: -5,
}]), []);

// 同一 uid 只出一行，且不修改传入的账号。
const dupRowAccounts = [
  {uid: 'dup', nickname: 'Dup', credits_expiry_buckets: [{expire_at: '2030-01-01T00:00:00Z', remain: 1}]},
  {uid: 'dup', nickname: 'Dup', credits_expiry_buckets: [{expire_at: '2030-02-01T00:00:00Z', remain: 2}]},
];
const dupRowBefore = JSON.stringify(dupRowAccounts);
check('duplicate uid yields one row', collectAccountExpiries(dupRowAccounts).length, 1);
check('does not mutate accounts with buckets', JSON.stringify(dupRowAccounts), dupRowBefore);

if (process.env.WB_TZ_CHILD !== '1') {
  const self = fileURLToPath(import.meta.url);
  for (const tz of ['UTC', 'Asia/Shanghai']) {
    const proc = spawnSync(
      process.execPath,
      ['--experimental-strip-types', self],
      {env: {...process.env, TZ: tz, WB_TZ_CHILD: '1'}, encoding: 'utf8'},
    );
    if (proc.status === 0) {
      console.log(`  ok  grouping in ${tz}`);
    } else {
      const out = `${proc.stdout || ''}${proc.stderr || ''}`;
      console.log(`  FAIL grouping in ${tz}\n${out}`);
      failed += 1;
    }
  }
}

console.log(failed ? `\n${failed} failed` : '\nall passed');
process.exit(failed ? 1 : 0);
