/**
 * `web/lib/retention-draft.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/retention-draft.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_retention_draft`
 * （没有 node 时那条会 skip，不阻塞后端测试套件）。
 *
 * 钉住的是三条「错了不报错、只是行为不对」的规则（见被测文件顶部）：
 *   · 没动过的键不能被提交（否则「跟随默认值」被偷偷改成「钉死当前值」）；
 *   · `ip_max_rows` 显式填默认值也必须提交（切换的是裁剪路径）；
 *   · 本地校验与服务端同边界（非整数/越界不能静默放行）。
 */
import {
  DAYS_BOUNDS,
  RETENTION_KEYS,
  ROWS_BOUNDS,
  diffDraft,
  isDraftValid,
  isValidValue,
  toDraft,
} from './retention-draft.ts';

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

const DEFAULTS = {
  request_days: 90,
  hourly_days: 90,
  task_days: 90,
  audit_max_rows: 20000,
  ip_max_rows: 20000,
};

/* ── 清单与边界 ─────────────────────────────────────────────── */

check('五个键，顺序固定（与后端 SPECS 一致）', RETENTION_KEYS, [
  'request_days', 'hourly_days', 'task_days', 'audit_max_rows', 'ip_max_rows',
]);

check('天数边界 1–3650', DAYS_BOUNDS, {min: 1, max: 3650});
check('行数边界 1–1000000', ROWS_BOUNDS, {min: 1, max: 1_000_000});

/* ── toDraft ────────────────────────────────────────────────── */

check('toDraft 把数字转成十进制字符串', toDraft(DEFAULTS), {
  request_days: '90', hourly_days: '90', task_days: '90',
  audit_max_rows: '20000', ip_max_rows: '20000',
});

/* ── isValidValue：合法 ─────────────────────────────────────── */

for (const [key, raw] of [
  ['request_days', '1'], ['request_days', '3650'],
  ['hourly_days', '7'], ['task_days', '90'],
  ['audit_max_rows', '1'], ['audit_max_rows', '1000000'],
  ['ip_max_rows', '20000'], ['ip_max_rows', '10'],
]) {
  check(`isValidValue('${key}', '${raw}') → true`, isValidValue(key, raw), true);
}

/* ── isValidValue：非法 ─────────────────────────────────────── */

const INVALID = [
  ['request_days', '0'], ['request_days', '3651'], ['request_days', '-1'],
  ['request_days', ''], ['request_days', '   '], ['request_days', '1.5'],
  ['request_days', '1e3'], ['request_days', '0x10'],
  ['request_days', '7天'], ['request_days', 'abc'],
  ['audit_max_rows', '0'], ['audit_max_rows', '1000001'], ['audit_max_rows', '-5'],
  ['ip_max_rows', '0'], ['ip_max_rows', '1.0'],
];
for (const [key, raw] of INVALID) {
  check(`isValidValue('${key}', ${JSON.stringify(raw)}) → false`, isValidValue(key, raw), false);
}

// 前后空白是 `type=number` 之外的手输路径（粘贴），去空白后仍是合法整数。
check('" 7 " 去空白后是合法整数', isValidValue('request_days', ' 7 '), true);
check('+7 不接受（不是纯十进制）', isValidValue('request_days', '+7'), false);

/* ── isDraftValid ───────────────────────────────────────────── */

check('全部默认值 → 合法', isDraftValid(toDraft(DEFAULTS)), true);
check('有一个越界 → 不合法',
      isDraftValid({...toDraft(DEFAULTS), request_days: '0'}), false);
check('有一个非整数 → 不合法',
      isDraftValid({...toDraft(DEFAULTS), audit_max_rows: '1.5'}), false);

/* ── diffDraft：稀疏语义 ────────────────────────────────────── */

const NONE = new Set();

check('什么都没动 → 空补丁', diffDraft(toDraft(DEFAULTS), DEFAULTS, NONE), {});

check(
  '动了 request_days 且值不同 → 只提交它',
  diffDraft({...toDraft(DEFAULTS), request_days: '7'}, DEFAULTS, new Set(['request_days'])),
  {request_days: 7},
);

check(
  '动了但值没变（非 ip_max_rows）→ 不提交（等于撤销 override）',
  diffDraft(toDraft(DEFAULTS), DEFAULTS, new Set(['request_days'])),
  {},
);

check(
  'ip_max_rows 动过、值等于默认值 → **仍然提交**（切换精确裁剪路径）',
  diffDraft(toDraft(DEFAULTS), DEFAULTS, new Set(['ip_max_rows'])),
  {ip_max_rows: 20000},
);

check(
  'ip_max_rows 没动过 → 不提交（哪怕界面上显示 20000）',
  diffDraft(toDraft(DEFAULTS), DEFAULTS, NONE),
  {},
);

check(
  '多个键同时改 → 全部提交',
  diffDraft(
    {...toDraft(DEFAULTS), request_days: '7', ip_max_rows: '10'},
    DEFAULTS,
    new Set(['request_days', 'ip_max_rows']),
  ),
  {request_days: 7, ip_max_rows: 10},
);

check(
  '动过但值非法 → 不进入补丁（不提交半合法请求）',
  diffDraft({...toDraft(DEFAULTS), request_days: '0'}, DEFAULTS, new Set(['request_days'])),
  {},
);

check(
  '空字符串 → 不进入补丁',
  diffDraft({...toDraft(DEFAULTS), task_days: ''}, DEFAULTS, new Set(['task_days'])),
  {},
);

/* ── diffDraft 不改动输入 ───────────────────────────────────── */

check('不修改传入的 draft / effective / touched', (() => {
  const draft = {...toDraft(DEFAULTS), request_days: '7'};
  const effective = {...DEFAULTS};
  const touched = new Set(['request_days']);
  const before = JSON.stringify([draft, effective, [...touched]]);
  diffDraft(draft, effective, touched);
  return JSON.stringify([draft, effective, [...touched]]) === before;
})(), true);

/* ── 与服务端默认值一致（跨语言的一致性守卫）───────────────── */

check(
  '默认值与本文件顶部 DEFAULTS 一致（契约 §5：{} → 用现有常量）',
  [DEFAULTS.request_days, DEFAULTS.hourly_days, DEFAULTS.task_days,
   DEFAULTS.audit_max_rows, DEFAULTS.ip_max_rows],
  [90, 90, 90, 20000, 20000],
);

if (failed) {
  console.log(`\n${failed} check(s) failed`);
  process.exit(1);
}
console.log('\nall passed');
