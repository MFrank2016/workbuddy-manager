/**
 * 密钥的产品 profile（迁移 Todo 20）：4 值 + 空 = 不限。
 *
 * 为什么单独一个模块：选择器、列表徽标、提交前的版本推导三处都要这份判据，
 * 写在页面里就是三份真相 —— 而「profile 与 realm 必须一致」正是那种**错了不
 * 报错、只是调用打空池**的规则（见 server/services/keyprofiles.py 的说明）。
 * 纯函数放这里，Node 直接跑（`web/lib/key-profile.test.mjs`）。
 *
 * 取值必须与 core 的 `auth.ResolveProfile` / manager 的 `keysvc.PROFILES`
 * 逐字一致：`cn-cli` / `cn-work` / `intl-cli` / `intl-work`。
 */
export const PROFILES = ['cn-cli', 'cn-work', 'intl-cli', 'intl-work'] as const;

export type Profile = (typeof PROFILES)[number];

/** 由 profile 推导 realm（`cn-*` → cn、`intl-*` → global）；认不出返回 ''。 */
export function profileRealm(profile: string | null | undefined): 'cn' | 'global' | '' {
  const name = String(profile ?? '').trim().toLowerCase();
  if (name.startsWith('intl-')) return 'global';
  if (name.startsWith('cn-')) return 'cn';
  return '';
}

/**
 * profile 与 realm 是否矛盾。返回 null = 一致（含任一为空 = 不限）。
 *
 * 与后端 `keyprofiles.realm_conflict` 同一口径：只校验**两者都非空**的组合。
 * 前端先拦是为了省一次往返，后端仍必须拦 —— 直接 POST 的路径不经过表单。
 */
export function profileRealmConflict(
  profile: string | null | undefined,
  realm: string | null | undefined,
): string | null {
  const prof = String(profile ?? '').trim().toLowerCase();
  const rlm = String(realm ?? '').trim().toLowerCase();
  if (!prof || !rlm) return null;
  const want = profileRealm(prof);
  if (!want || want === rlm) return null;
  return `profile ${prof} belongs to ${want}, which conflicts with the pinned edition ${rlm}`;
}

/** 归一化表单里的 profile：非法/空 → ''（= 不限）。 */
export function normalizeProfile(value: unknown): Profile | '' {
  const v = String(value ?? '').trim().toLowerCase();
  return (PROFILES as readonly string[]).includes(v) ? (v as Profile) : '';
}
