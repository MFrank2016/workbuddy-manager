/**
 * `web/lib/log-detail.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/log-detail.test.mjs
 *
 * 钉住的是日志详情抽屉里几条「错了不报错、只是会误导人」的规则：
 *
 *   · `credit=null`（上游没返回）与 `credit=0`（这次确实没扣费）**不能长得一样**
 *     —— 抽屉里 null 必须显示「—」，0 必须显示「0」；
 *   · `tool_repair` 的四种结果必须各自有本地化标签且颜色分档不同，
 *     未知值原样显示而不是渲染成空白；
 *   · 缺失字段（老后端 / 老记录）不能让整个抽屉崩掉，也不能显示 `undefined`；
 *   · 超长 `error` / `ua` 原样保留（不截断——排查时要能整段复制走）。
 *
 * 翻译函数是**注入**的（模块本身零运行时依赖），所以这里可以直接断言
 * 「用了哪个键」，不必启动 React 或读语言文件。
 */
import {creditText, latencyText, logDetailRows, toolRepairLabel, toolRepairTone} from './log-detail.ts';

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

/** 假翻译：返回带方括号的键名，一眼能看出取的是哪条文案。 */
const t = (key) => `[${key}]`;

/** 取某一行（按稳定 id）。找不到时返回 undefined，让断言直接报出来。 */
const rowOf = (rows, id) => rows.find((r) => r.id === id);
const valueOf = (rows, id) => rowOf(rows, id)?.value;

/* ── credit：null ≠ 0（本任务的核心规则） ─────────────────────── */
console.log('creditText：');
check('null → 破折号', creditText(null), '—');
check('undefined（字段缺失）→ 破折号', creditText(undefined), '—');
check('0 → 字面 0（不是破折号）', creditText(0), '0');
check('小数保留 4 位', creditText(0.0312), '0.0312');
check('整数金额保留 2 位', creditText(2), '2.00');
check('大额取整并带千分位（en）', creditText(1234.6, 'en'), '1,235');
check('大额取整并带千分位（de）', creditText(1234.6, 'de'), '1.235');
check('NaN 不显示 NaN', creditText(Number.NaN), '—');
check('Infinity 不显示 Infinity', creditText(Infinity), '—');

/* ── 首字延迟 ─────────────────────────────────────────────────── */
console.log('latencyText：');
check('null → 破折号', latencyText(null), '—');
check('0ms 仍然是 0ms（不是破折号）', latencyText(0), '0ms');
check('不足 1 秒用 ms', latencyText(860), '860ms');
check('超过 1 秒用 s', latencyText(2500), '2.50s');

/* ── tool_repair：四种结果各自可辨 ────────────────────────────── */
console.log('toolRepairLabel / tone：');
const REPAIRS = ['none', 'fixed', 'failed', 'recalled'];
const labels = REPAIRS.map((v) => toolRepairLabel(v, t));
check('四种结果的标签互不相同', new Set(labels).size, 4);
check('none 用 logs.toolRepairNone', toolRepairLabel('none', t), '[logs.toolRepairNone]');
check('fixed 用 logs.toolRepairFixed', toolRepairLabel('fixed', t), '[logs.toolRepairFixed]');
check('failed 用 logs.toolRepairFailed', toolRepairLabel('failed', t), '[logs.toolRepairFailed]');
check('recalled 用 logs.toolRepairRecalled', toolRepairLabel('recalled', t), '[logs.toolRepairRecalled]');
check('null 视为 none（旧记录）', toolRepairLabel(null, t), '[logs.toolRepairNone]');
check('未知值原样显示（不渲染空白）', toolRepairLabel('weird', t), 'weird');
check('四种结果的色调互不相同', new Set(REPAIRS.map(toolRepairTone)).size, 4);
check('fixed 是「好」的色调', toolRepairTone('fixed'), 'ok');
check('failed 是「坏」的色调', toolRepairTone('failed'), 'bad');
check('recalled 是「警告」的色调', toolRepairTone('recalled'), 'warn');
check('none 是中性', toolRepairTone('none'), 'neutral');
check('未知值中性', toolRepairTone('weird'), 'neutral');

/* ── 整行：字段齐全 ───────────────────────────────────────────── */
console.log('logDetailRows：');
const full = {
  id: 42,
  ip: '10.0.0.7',
  key_name: 'demo-key',
  account: '张叔叔(299e342b)',
  realm: 'global',
  model: 'glm-5.2',
  mapped_model: 'glm-5.2-upstream',
  status: 502,
  first_token_ms: 0,
  latency_ms: 1200,
  prompt_tokens: 8000,
  completion_tokens: 100,
  cache_hit_tokens: 0,
  cache_miss_tokens: 900,
  cache_write_tokens: null,
  credit: 0,
  stream: true,
  tool_repair: 'fixed',
  ua: 'curl/8.5.0',
  error: 'upstream 502 bad gateway',
};
const fullRows = logDetailRows(full, {t, n: (v) => String(v)});
check('每行的 id 唯一', new Set(fullRows.map((r) => r.id)).size, fullRows.length);
check('包含本任务要求的全部字段', fullRows.map((r) => r.id), [
  'ip', 'key', 'account', 'realm', 'model', 'mapped', 'status',
  'firstToken', 'latency', 'promptTokens', 'completionTokens',
  'cacheHit', 'cacheMiss', 'cacheWrite', 'credit', 'stream', 'toolRepair', 'ua', 'error',
]);
check('realm=global 用国际版文案', valueOf(fullRows, 'realm'), '[realm.global]');
check('tool_repair=fixed 用 fixed 文案', valueOf(fullRows, 'toolRepair'), '[logs.toolRepairFixed]');
check('tool_repair 行带「好」的色调', rowOf(fullRows, 'toolRepair').tone, 'ok');
check('credit=0 显示 0', valueOf(fullRows, 'credit'), '0');
check('cache 命中 0 显示 0（不是未采集）', valueOf(fullRows, 'cacheHit'), '0');
check('cache 写入 null 显示未采集（通用文案，不说是非流式）', valueOf(fullRows, 'cacheWrite'), '[logs.notCollectedShort]');
check('first_token=0 显示 0ms', valueOf(fullRows, 'firstToken'), '0ms');
check('mapped_model 显示', valueOf(fullRows, 'mapped'), 'glm-5.2-upstream');
check('stream=true 显示「是」', valueOf(fullRows, 'stream'), '[common.yes]');
check('error 原文显示', valueOf(fullRows, 'error'), 'upstream 502 bad gateway');
check('error 可复制', rowOf(fullRows, 'error').copyable, true);
check('ua 可复制', rowOf(fullRows, 'ua').copyable, true);
check('ua/error 标为可滚动长文本', [rowOf(fullRows, 'ua').scroll, rowOf(fullRows, 'error').scroll], [true, true]);
check('标签全部来自翻译（没有写死文案）', fullRows.every((r) => r.label.startsWith('[')), true);
check('不修改传入的日志对象', JSON.stringify(full) === JSON.stringify({
  id: 42, ip: '10.0.0.7', key_name: 'demo-key', account: '张叔叔(299e342b)',
  realm: 'global', model: 'glm-5.2', mapped_model: 'glm-5.2-upstream', status: 502,
  first_token_ms: 0, latency_ms: 1200, prompt_tokens: 8000, completion_tokens: 100,
  cache_hit_tokens: 0, cache_miss_tokens: 900, cache_write_tokens: null, credit: 0,
  stream: true, tool_repair: 'fixed', ua: 'curl/8.5.0', error: 'upstream 502 bad gateway',
}), true);

/* ── 整行：字段缺失（老后端 / 老记录） ───────────────────────── */
console.log('缺失字段降级：');
const sparseRows = logDetailRows({id: 7}, {t});
check('缺 credit → 破折号（不是 undefined）', valueOf(sparseRows, 'credit'), '—');
check('缺 realm → 按国内版归类', valueOf(sparseRows, 'realm'), '[realm.cn]');
check('缺 tool_repair → none', valueOf(sparseRows, 'toolRepair'), '[logs.toolRepairNone]');
check('缺 tool_repair 色调中性', rowOf(sparseRows, 'toolRepair').tone, 'neutral');
check('缺 account → 尚未采集', valueOf(sparseRows, 'account'), '[logs.accountPending]');
check('缺 first_token → 未采集（非流式）', valueOf(sparseRows, 'firstToken'), '[logs.notCollected]');
check('cache 缺字段 → 通用未采集文案', [valueOf(sparseRows, 'cacheHit'), valueOf(sparseRows, 'cacheMiss'), valueOf(sparseRows, 'cacheWrite')], ['[logs.notCollectedShort]', '[logs.notCollectedShort]', '[logs.notCollectedShort]']);
check('缺 error/ua → 破折号', [valueOf(sparseRows, 'error'), valueOf(sparseRows, 'ua')], ['—', '—']);
check('缺 error 不可复制', rowOf(sparseRows, 'error').copyable, false);
check('缺 stream → 显示「否」（不是 undefined）', valueOf(sparseRows, 'stream'), '[common.no]');
check('缺 mapped_model → 破折号', valueOf(sparseRows, 'mapped'), '—');
check('缺 model → 破折号', valueOf(sparseRows, 'model'), '—');
check('缺 key_name → 破折号', valueOf(sparseRows, 'key'), '—');
check('空字符串也不渲染成空白', logDetailRows({id: 8, ip: '', error: '', ua: ''}, {t})
  .filter((r) => ['ip', 'error', 'ua'].includes(r.id))
  .map((r) => r.value), ['—', '—', '—']);

/* ── 超长 error / ua：原样保留，不截断 ───────────────────────── */
console.log('超长文本：');
const longError = 'E'.repeat(20000) + '\n  at upstream (gateway.py:424)';
const longRows = logDetailRows({id: 9, error: longError, ua: 'U'.repeat(9000)}, {t});
check('超长 error 完整保留（不截断）', valueOf(longRows, 'error').length, longError.length);
check('超长 error 内容逐字相同', valueOf(longRows, 'error') === longError, true);
check('超长 ua 完整保留', valueOf(longRows, 'ua').length, 9000);
check('超长文本仍可复制', rowOf(longRows, 'error').copyable, true);

/* ── 数字格式化由调用方注入（页面传 fmtNumber） ─────────────── */
console.log('注入的数字格式化：');
const numbered = logDetailRows({id: 10, prompt_tokens: 8000, completion_tokens: 0}, {
  t,
  n: (v) => `#${v}`,
});
check('prompt token 走注入的格式化', valueOf(numbered, 'promptTokens'), '#8000');
check('completion token 0 也走注入的格式化', valueOf(numbered, 'completionTokens'), '#0');

/* ── 默认依赖：不传 n 也不能渲染出 undefined ────────────────── */
const defaultRows = logDetailRows({id: 11, prompt_tokens: 12}, {t});
check('不传 n 时用原始数字串', valueOf(defaultRows, 'promptTokens'), '12');
const localeRows = logDetailRows({id: 12, credit: 1234.6}, {t, locale: 'en'});
check('行里的金额跟随注入的语言（千分位）', valueOf(localeRows, 'credit'), '1,235');
check('没有 undefined 漏到界面', defaultRows.every((r) => !String(r.value).includes('undefined')), true);

if (failed) {
  console.log(`\n${failed} failed`);
  process.exit(1);
}
console.log('\nall passed');
