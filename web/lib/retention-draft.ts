/**
 * 日志保留策略表单的**纯逻辑**（零依赖，Node 可直接跑 .ts）。
 *
 * 为什么从组件里拆出来：这里有三条「错了不报错、只是行为不对」的规则，
 * 只有把它们做成可单测的纯函数才守得住——
 *
 *   1. **只提交动过的键**。把没动过的键也提交，等于把「跟随系统默认值」
 *      偷偷改成「钉死在当前值」；以后调整默认常量，这台机器不再跟着变。
 *   2. **`ip_max_rows` 显式等于默认值也要提交**。它切换的是裁剪路径（有 override
 *      → 每次写入精确裁到 N 行；没有 → 每 500 次检查、留 10% 余量），只看
 *      「值变没变」就永远表达不了这个选择。
 *   3. **本地校验与服务端同边界**。天数 1–3650、行数 1–1 000 000，且必须是整数
 *      ——`Number('')` 是 0、`Number('1e3')` 是 1000，不显式挡就会被静默接受。
 *
 * 不 import 任何东西（与 `settings-tabs.ts` 同一条约束）：`web/lib/*.test.mjs`
 * 直接 import 本文件，引入运行时 import 会让测试以「找不到模块」失败。
 */

export type RetentionKey =
  | 'request_days'
  | 'hourly_days'
  | 'task_days'
  | 'audit_max_rows'
  | 'ip_max_rows';

export const RETENTION_KEYS: readonly RetentionKey[] = [
  'request_days',
  'hourly_days',
  'task_days',
  'audit_max_rows',
  'ip_max_rows',
] as const;

/** 校验边界。与后端 `server/retention.py` 的 DAYS_* / ROWS_* 逐字一致。 */
export const DAYS_BOUNDS = {min: 1, max: 3650} as const;
export const ROWS_BOUNDS = {min: 1, max: 1_000_000} as const;

export type Draft = Record<RetentionKey, string>;
export type Patch = Partial<Record<RetentionKey, number>>;

export function boundsOf(key: RetentionKey): {min: number; max: number} {
  return key === 'request_days' || key === 'hourly_days' || key === 'task_days'
    ? DAYS_BOUNDS
    : ROWS_BOUNDS;
}

/** 生效值 → 输入框里的字符串（一律十进制、无单位、无千分位）。 */
export function toDraft(effective: Record<RetentionKey, number>): Draft {
  return Object.fromEntries(
    RETENTION_KEYS.map((key) => [key, String(effective[key])]),
  ) as Draft;
}

/**
 * 单个输入是否合法：非空、纯十进制整数、在边界内。
 *
 * 用正则而不是 `Number()` 就够：`Number(' 7 ')` 是 7、`Number('0x10')` 是 16、
 * `Number('')` 是 0 —— 这些都会被当作合法值提交，而用户看到的字符串不是那个数。
 * `type=number` 的输入框在多数浏览器里也会把非法内容变成空串，两者都挡。
 */
export function isValidValue(key: RetentionKey, raw: string): boolean {
  const text = raw.trim();
  if (!/^\d+$/.test(text)) return false;
  const n = Number(text);
  const {min, max} = boundsOf(key);
  return n >= min && n <= max;
}

/** 整个表单是否全部合法（任一不合法就不该允许保存）。 */
export function isDraftValid(draft: Draft): boolean {
  return RETENTION_KEYS.every((key) => isValidValue(key, draft[key]));
}

/**
 * 要提交的补丁：**只含用户动过、且这次值有意义的键**。
 *
 * `touched` 是用户编辑过的键集合（不是「值变了的键集合」）。
 * 见本文件顶部第 1、2 条规则。
 */
export function diffDraft(
  draft: Draft,
  effective: Record<RetentionKey, number>,
  touched: ReadonlySet<RetentionKey>,
): Patch {
  const out: Patch = {};
  for (const key of RETENTION_KEYS) {
    if (!touched.has(key)) continue;
    if (!isValidValue(key, draft[key])) continue;
    const same = draft[key].trim() === String(effective[key]);
    // 值没变：只有 ip_max_rows 还需要提交（它切换的是路径，不是数值）。
    if (same && key !== 'ip_max_rows') continue;
    out[key] = Number(draft[key].trim());
  }
  return out;
}
