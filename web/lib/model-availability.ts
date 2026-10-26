/**
 * 模型可用性的**单一判定来源**（模型中心的表格与移动卡片共用）。
 *
 * 语义参考上游面板的 `mdAvailabilityOf`，但这里多了一步：manager 的模型目录
 * 保留 `global:` 前缀（国内模型是裸名），而锁池/请求事件里的 `model` 是**裸名**。
 * 所以必须先剥掉 id 的 realm 前缀，否则国际版模型永远匹配不到自己的锁，
 * 明明不可用却显示「可用」——这类错误不会抛异常，只会安静地给错徽标。
 *
 * 判定优先级（不可随意调整）：
 *   锁命中（11158 → denied，其它 → blocked） → 最近同模型事件成功 → ok → null
 *
 * ⚠️ 本模块**不得引入任何运行时 import**（只有 `import type`，编译后被抹掉）。
 * `web/lib/*.test.mjs` 用 `node --experimental-strip-types` 直接 import `.ts`
 * 源码，Node 的 ESM 解析要求带扩展名，而仓库的 app 代码一律不写扩展名——
 * 一旦这里 import 了别的模块的**值**，测试就会以「找不到模块」失败。
 */

/** 模型当前的可用性；`null` 表示未知（不显示徽标） */
export type ModelAvailability = 'denied' | 'blocked' | 'ok';

/** 锁池条目：只读这里用到的字段，避免依赖尚未落地的共享类型 */
interface LockRowLike {
  model?: unknown;
  realm?: unknown;
  reason?: unknown;
}

/** 请求事件：只读这里用到的字段 */
interface RecentEventLike {
  model?: unknown;
  ok?: unknown;
}

/** 目录里保留的 realm 前缀（国内为裸名，国际版带 `global:`） */
const REALM_PREFIX = /^(?:cn|global):/i;

/**
 * 判断一个模型当前是否可用。
 *
 * @param id     模型 id，可能带 `global:` 前缀
 * @param realm  当前查看的域（`cn` / `global`）
 * @param locks  锁池条目（可为 null/undefined）
 * @param recent 最近请求事件，按时间升序（最后一条最新）
 */
export function modelAvailabilityOf(
  id: string | null | undefined,
  realm: string | null | undefined,
  locks: readonly LockRowLike[] | null | undefined,
  recent: readonly RecentEventLike[] | null | undefined,
): ModelAvailability | null {
  const bare = String(id ?? '').replace(REALM_PREFIX, '');
  const wantRealm = String(realm ?? '').toLowerCase();

  const lockRows = Array.isArray(locks) ? locks : [];
  for (const row of lockRows) {
    if (!row || String(row.model ?? '') !== bare) continue;
    const rowRealm = String(row.realm ?? '').toLowerCase();
    if (wantRealm && rowRealm && rowRealm !== wantRealm) continue;
    const reason = String(row.reason ?? '').toLowerCase();
    return reason.startsWith('11158') ? 'denied' : 'blocked';
  }

  const events = Array.isArray(recent) ? recent : [];
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (!event || String(event.model ?? '') !== bare) continue;
    return event.ok === true ? 'ok' : null;
  }

  return null;
}
