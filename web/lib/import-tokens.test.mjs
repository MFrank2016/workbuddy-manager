/**
 * `web/lib/import-tokens.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types --test web/lib/*.test.mjs
 *
 * 钉住的是「提交前必须查出来」的那几件事——它们全都**不能**留给后端：
 *
 *   · 非法 JSON：本地就报错，不能发一次必然 400 的请求；
 *   · 超过 200 条：后端的硬上限，先拦下来比让 200 条各失败一次好；
 *   · 重复 uid：后端只会按顺序各写一遍（并发 2，谁最后落盘不确定），
 *     用户看到「成功」却不知道留下了哪个 token —— 这是最危险的一类静默错误；
 *   · 空输入 / 全空行：提交按钮必须是禁用的，而不是点了没反应。
 *
 * 另外钉住「两种输入形态」：完整 auth JSON 的数组（含 `{account, auth}` 嵌套）
 * 与每行 `uid,refresh_token` 的粘贴文本。后者是用户从表格里复制出来的实际形态。
 */
import {
  MAX_ENTRIES,
  buildImportPayload,
  parseImportText,
} from './import-tokens.ts';

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

const ok = (r) => r.fatal === null && r.entries.length > 0
  && r.errors.length === 0 && r.duplicates.length === 0;

/* ── JSON 数组（扁平条目）──────────────────────────────── */

{
  const r = parseImportText(
    '[{"uid":"u1","refresh_token":"r1"},{"uid":"u2","refreshToken":"r2"}]');
  check('JSON 数组：两条都解析出来', r.entries.length, 2);
  check('JSON 数组：uid 按原序', r.uids, ['u1', 'u2']);
  check('JSON 数组：可提交', ok(r), true);
  check('JSON 数组：refresh_token 与 refreshToken 都认',
    [r.entries[0].refresh_token, r.entries[1].refreshToken], ['r1', 'r2']);
}

/* ── 完整 auth JSON 的嵌套形 `{account, auth}` ─────────── */

{
  const text = JSON.stringify([
    {
      account: {uid: 'acct-1', nickname: '演示号'},
      auth: {refreshToken: 'rt-1', accessToken: 'ignored', realm: 'cn'},
    },
    {
      account: {user_id: 'acct-2'},
      auth: {refresh_token: 'rt-2'},
    },
  ]);
  const r = parseImportText(text);
  check('嵌套形：uid 取自 account.uid / account.user_id', r.uids, ['acct-1', 'acct-2']);
  check('嵌套形：refresh 取自 auth.refreshToken / auth.refresh_token',
    [r.entries[0].auth.refreshToken, r.entries[1].auth.refresh_token], ['rt-1', 'rt-2']);
  check('嵌套形：可提交', ok(r), true);
}

/* ── 单个对象 / `{entries:[...]}` 包装也认 ─────────────── */

{
  const one = parseImportText('{"uid":"solo","refresh_token":"r"}');
  check('单个 JSON 对象当作一条', one.uids, ['solo']);
  check('单个对象：可提交', ok(one), true);

  const wrapped = parseImportText('{"entries":[{"uid":"w1","refresh_token":"r1"}]}');
  check('{entries:[...]} 包装取 entries', wrapped.uids, ['w1']);
  check('包装形：可提交', ok(wrapped), true);
}

/* ── 每行 `uid,refresh_token`（空行与空白要容忍）───────── */

{
  const r = parseImportText('\n  u1, r1  \n\n\tu2 ,r2\n\n');
  check('CSV：两条、跳过空行', r.entries.length, 2);
  check('CSV：uid 去掉首尾空白', r.uids, ['u1', 'u2']);
  check('CSV：token 去掉首尾空白',
    [r.entries[0].refresh_token, r.entries[1].refresh_token], ['r1', 'r2']);
  check('CSV：可提交', ok(r), true);
}

{
  // token 里本身可能带逗号（JWT 的 base64 段不会有，但粘贴的 JSON 片段会有），
  // 所以只按**第一个**逗号切分，剩下的整段都是 token。
  const r = parseImportText('u1,abc,def');
  check('CSV：只按第一个逗号切分', r.entries[0].refresh_token, 'abc,def');
}

{
  const r = parseImportText('"u1","r1"\nu2,r2');
  check('CSV：容忍表格粘贴带引号', r.uids, ['u1', 'u2']);
  check('CSV：引号被去掉', r.entries[0].refresh_token, 'r1');
}

/* ── 空输入 ─────────────────────────────────────────────── */

check('空串 → fatal=empty', parseImportText('').fatal, 'empty');
check('全空白 → fatal=empty', parseImportText('  \n\n \t ').fatal, 'empty');
check('空 JSON 数组 → fatal=empty', parseImportText('[]').fatal, 'empty');
check('空数组没有可提交条目', ok(parseImportText('[]')), false);

/* ── 非法 JSON 必须本地拦下（不能发请求）─────────────────── */

{
  const r = parseImportText('[{"uid":"u1",}]');
  check('坏 JSON → fatal=invalid_json', r.fatal, 'invalid_json');
  check('坏 JSON：没有任何条目可提交', r.entries.length, 0);
  check('坏 JSON：不可提交', ok(r), false);
}

{
  const r = parseImportText('{"uid":"u1"');
  check('未闭合对象 → fatal=invalid_json', r.fatal, 'invalid_json');
}

{
  // 首字符不是 `[` / `{` → 走「每行一条」，一行又没有逗号 → 字段校验拦下。
  // 判定只看首字符，不去猜「这看起来像数字所以是坏 JSON」：猜错一次就会把
  // 合法的 CSV 行（uid 恰好是纯数字）当成 JSON 拒掉。
  const r = parseImportText('42');
  check('纯数字单行 → reason=invalid_entry', r.errors[0]?.reason, 'invalid_entry');
  check('纯数字单行：不可提交', ok(r), false);
}

/* ── 200 条上限（与 tokenimport.MAX_ENTRIES 一致）────────── */

check('MAX_ENTRIES 与后端一致（200）', MAX_ENTRIES, 200);

{
  const many = Array.from({length: MAX_ENTRIES + 1},
    (_, i) => `u${i},r${i}`).join('\n');
  const r = parseImportText(many);
  check('201 条 → fatal=too_many', r.fatal, 'too_many');
  check('201 条：不可提交', ok(r), false);
  check('201 条：count 报出真实条数', r.count, MAX_ENTRIES + 1);
}

{
  const exact = Array.from({length: MAX_ENTRIES},
    (_, i) => `u${i},r${i}`).join('\n');
  const r = parseImportText(exact);
  check('正好 200 条 → 可提交', ok(r), true);
  check('正好 200 条：count=200', r.count, MAX_ENTRIES);
}

{
  // JSON 数组同样受上限约束：不能因为「是 JSON」就绕过。
  const many = JSON.stringify(Array.from({length: MAX_ENTRIES + 1},
    (_, i) => ({uid: `u${i}`, refresh_token: `r${i}`})));
  check('JSON 数组 201 条 → fatal=too_many', parseImportText(many).fatal, 'too_many');
}

/* ── 重复 uid ───────────────────────────────────────────── */

{
  const r = parseImportText('u1,r1\nu2,r2\nu1,r3');
  check('重复 uid 被报出来（每个只报一次）', r.duplicates, ['u1']);
  check('重复 uid：不可提交', ok(r), false);
  check('重复 uid：仍然解析出 3 条', r.entries.length, 3);

  const tri = parseImportText('u1,r1\nu1,r2\nu1,r3');
  check('同一 uid 出现三次也只报一次', tri.duplicates, ['u1']);
}

{
  const r = parseImportText(JSON.stringify([
    {account: {uid: 'dup'}, auth: {refreshToken: 'a'}},
    {account: {uid: 'dup'}, auth: {refreshToken: 'b'}},
  ]));
  check('嵌套形里的重复 uid 同样报出来', r.duplicates, ['dup']);
}

/* ── 逐条字段校验（提前给出原因，别等后端逐条 rejected）──── */

{
  const r = parseImportText('[{"refresh_token":"r1"}]');
  check('缺 uid → reason=missing_uid', r.errors[0]?.reason, 'missing_uid');
  check('缺 uid：不可提交', ok(r), false);
}

{
  const r = parseImportText('[{"uid":"u1"}]');
  check('缺 refresh token → reason=missing_refresh_token',
    r.errors[0]?.reason, 'missing_refresh_token');
}

{
  const r = parseImportText('[{"uid":"bad uid!","refresh_token":"r"}]');
  check('uid 形态非法 → reason=invalid_uid', r.errors[0]?.reason, 'invalid_uid');
}

{
  const r = parseImportText('["not-an-object"]');
  check('条目不是对象 → reason=invalid_entry', r.errors[0]?.reason, 'invalid_entry');
}

{
  const r = parseImportText('no-comma-here');
  check('CSV 行没有逗号 → reason=invalid_entry', r.errors[0]?.reason, 'invalid_entry');
  check('CSV 行没有逗号：不可提交', ok(r), false);
}

/* ── 大小写 / BOM / 首尾空白 ────────────────────────────── */

{
  const r = parseImportText('\uFEFF[{"uid":"u1","refresh_token":"r1"}]');
  check('带 BOM 的 JSON 仍能解析', r.uids, ['u1']);
}

{
  const r = parseImportText('   [{"uid":"u1","refresh_token":"r1"}]  \n');
  check('首尾空白不影响 JSON 判定', r.uids, ['u1']);
}

/* ── 提交体（realm / upstream / overwrite 的落点）────────── */

{
  const parsed = parseImportText('u1,r1\nu2,r2');
  const body = buildImportPayload(parsed, {realm: 'global', upstreamId: 7, overwrite: true});
  check('提交体：realm 原样带上', body.realm, 'global');
  check('提交体：upstream_id 是数字', body.upstream_id, 7);
  check('提交体：overwrite 是布尔', body.overwrite, true);
  check('提交体：entries 长度 2', body.entries.length, 2);

  const def = buildImportPayload(parsed, {realm: 'cn', upstreamId: null, overwrite: false});
  check('默认分组：不带 upstream_id 键', 'upstream_id' in def, false);
  check('默认分组：overwrite=false', def.overwrite, false);
}

{
  // 显式 `undefined` 与 null 同义：都不该出现 `upstream_id: undefined` 这种键。
  const parsed = parseImportText('u1,r1');
  const body = buildImportPayload(parsed, {realm: 'cn', upstreamId: undefined, overwrite: false});
  check('upstreamId=undefined 同样不带键', Object.keys(body).sort(),
    ['entries', 'overwrite', 'realm']);
}

if (failed) {
  console.log(`\n${failed} check(s) failed`);
  process.exit(1);
}
console.log('\nall passed');
