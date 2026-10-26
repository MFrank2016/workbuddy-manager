/**
 * `modelAvailabilityOf` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types --test web/lib/model-availability.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_model_availability`
 * （没有 node 时那条会 skip，不会阻塞后端测试套件）。
 *
 * 为什么值得单独测：这个函数错起来**界面不报错**，只是徽标凭空出现或消失——
 *   · 不剥 `global:` 前缀 → 国际版模型永远匹配不到锁，明明不可用却显示「可用」；
 *   · 忽略 realm → 国内版的锁串到国际版模型上（或反之）；
 *   · 取「第一条」而不是「最新一条」最近事件 → 恢复后仍显示失败，或失败后仍显示可用；
 *   · 把 11158（无权限）混成普通限流 → 用户看到「不可用」却不知道是没权限。
 * 这些都不会抛异常，只会让用户看到一个**看起来很正常**的错徽标。
 */
import {modelAvailabilityOf} from './model-availability.ts';

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

const lock = (model, realm, reason) => ({model, realm, reason});
const event = (model, ok) => ({model, ok});

console.log('modelAvailabilityOf：锁池命中 → denied/blocked；最近成功 → ok；否则 null');

check(
  '11158 开头的锁 → denied（无权限，不是普通限流）',
  modelAvailabilityOf('m', 'cn', [lock('m', 'cn', '11158 no permission')], []),
  'denied',
);
check(
  '跨域不串：cn 模型不命中 global 的锁',
  modelAvailabilityOf('m', 'cn', [lock('m', 'global', '11102 rate limited')], []),
  null,
);
check(
  '其它锁 → blocked',
  modelAvailabilityOf('m', 'cn', [lock('m', 'cn', '11102 rate limited')], []),
  'blocked',
);
check(
  '最近同模型成功 → ok',
  modelAvailabilityOf('m', 'cn', [], [event('m', true)]),
  'ok',
);
check(
  '最新一条是失败 → null（即使更早有一条成功）',
  modelAvailabilityOf('m', 'cn', [], [event('m', true), event('m', false)]),
  null,
);
check(
  '没有锁也没有最近事件 → null（未知，不显示徽标）',
  modelAvailabilityOf('m', 'cn', [], []),
  null,
);
check(
  'global: 前缀的 id 剥前缀后命中 global 锁',
  modelAvailabilityOf(
    'global:gpt-5.6-sol',
    'global',
    [lock('gpt-5.6-sol', 'global', '11102 rate limited')],
    [],
  ),
  'blocked',
);

console.log(failed ? `\n${failed} failed` : '\nall passed');
process.exit(failed ? 1 : 0);
