/**
 * `web/lib/key-profile.ts` 的行为测试（Node 直接跑 .ts 源码）。
 *
 * 跑法：
 *     node --experimental-strip-types web/lib/key-profile.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_key_profile_web`
 *
 * 钉住的是「错了不报错、只是调用打空池」的那条规则：
 * profile 与 realm 必须一致（cn-* ↔ cn、intl-* ↔ global）。
 */
import {
  PROFILES,
  normalizeProfile,
  profileRealm,
  profileRealmConflict,
} from './key-profile.ts';

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

check('four profiles, no more', PROFILES, ['cn-cli', 'cn-work', 'intl-cli', 'intl-work']);

check('cn-cli realm', profileRealm('cn-cli'), 'cn');
check('cn-work realm', profileRealm('cn-work'), 'cn');
check('intl-cli realm', profileRealm('intl-cli'), 'global');
check('intl-work realm', profileRealm('intl-work'), 'global');
check('empty profile has no realm', profileRealm(''), '');
check('unknown profile has no realm', profileRealm('weird'), '');
check('case and whitespace normalized', profileRealm(' INTL-Work '), 'global');

check('agreeing pair passes (cn)', profileRealmConflict('cn-work', 'cn'), null);
check('agreeing pair passes (global)', profileRealmConflict('intl-work', 'global'), null);
check('conflicting pair rejected', profileRealmConflict('intl-cli', 'cn') !== null, true);
const conflictMsg = profileRealmConflict('intl-cli', 'cn');
check('conflict mentions profile', String(conflictMsg).includes('intl-cli'), true);
check('conflict mentions realm', String(conflictMsg).includes('cn'), true);
check('empty profile is unrestricted', profileRealmConflict('', 'cn'), null);
check('empty realm is unrestricted', profileRealmConflict('cn-cli', ''), null);
check('unknown profile never invents conflict', profileRealmConflict('weird', 'cn'), null);

check('normalize keeps valid', normalizeProfile('CN-Work'), 'cn-work');
check('normalize drops invalid', normalizeProfile('cn-desktop'), '');
check('normalize drops empty', normalizeProfile(''), '');
check('normalize drops null', normalizeProfile(null), '');

if (failed) {
  console.log(`\n${failed} check(s) failed`);
  process.exit(1);
}
console.log('\nall passed');
