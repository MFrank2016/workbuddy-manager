/**
 * 请求日志详情抽屉的**纯逻辑**（零运行时依赖，Node 可直接跑 .ts）。
 *
 * 为什么从页面里拆出来：抽屉里有几条「错了不报错、只是会误导人」的规则，
 * 只有做成可单测的纯函数才守得住——
 *
 *   1. **`credit=null` 与 `credit=0` 不能长得一样**。前者是上游没返回这个字段
 *      （老上游 / 非计费请求），后者是这次调用确实没扣费。都显示 0 会让用户
 *      以为「免费」，都显示「—」又会让真实的零扣费看起来像没数据。
 *   2. **`tool_repair` 的四种结果要各自可辨**（none / fixed / failed / recalled），
 *      并且颜色分档不同 —— 只写一个布尔值会把「修复成功」和「修复失败」混成
 *      「动过参数」，排查工具调用问题时正好丢掉最关键的信息。
 *   3. **缺字段不能崩**。抽屉的数据来自列表接口，老后端不返回 `tool_repair`、
 *      老记录没有 `realm` / `cache_*`；缺哪个就降级哪个，不能整块渲染成
 *      `undefined` 或让页面白屏。
 *   4. **超长 `error` / `ua` 原样保留**。排查时要能整段复制走，截断等于把
 *      证据丢掉 —— 换行与滚动交给 CSS（`break-all` + `max-h` + `overflow-auto`）。
 *
 * 翻译函数与数字格式化**注入**进来（`deps.t` / `deps.n`）：本模块不 import
 * `@/lib/i18n`，`web/lib/*.test.mjs` 才能用 Node 的 type stripping 直接跑它。
 * 与 `retention-draft.ts` / `usage-chart.ts` 同一条约束。
 */
import type {RequestLog} from './types';

/** 抽屉里一行「标签 → 值」。 */
export interface LogDetailRow {
  /** 稳定 id：React key 与测试定位都用它，不随文案变化。 */
  id: string;
  label: string;
  value: string;
  /**
   * 值的语义色调（由调用方决定怎么画）：
   * `ok` 正常 / `warn` 需留意 / `bad` 失败 / `neutral` 中性。
   * 只有 `tool_repair` 与 `credit` 用得上，其余行留空。
   */
  tone?: LogTone;
  /** 长文本且需要贴出去（IP / UA / 错误）：给出复制入口。 */
  copyable?: boolean;
  /** 可能极长的文本：需要换行 + 限高滚动，不能撑破抽屉宽度。 */
  scroll?: boolean;
}

export type LogTone = 'neutral' | 'ok' | 'warn' | 'bad';

/** 依赖注入：翻译 + 数字格式化（默认退回原始数字串）+ 金额的语言。 */
export interface LogDetailDeps {
  t: (key: string) => string;
  n?: (value: number) => string;
  /** BCP-47 语言标签（金额的千分位分隔符随语言变化）。不传则跟随运行环境。 */
  locale?: string;
}

/** 日志详情能拿到的字段；列表接口返回的 `RequestLog` 是它的超集。 */
export type LogDetailSource = Partial<RequestLog> & {id: number};

/**
 * 扣费金额格式化（上游 usage.credit）。
 *
 * 单次调用常是 0.0x 量级，直接 `toLocaleString` 会显示成 0，因此小数值保留
 * 最多 4 位有效小数；整数则按千分位显示。**null 与 0 严格分开**：
 * null（含字段缺失、NaN、Infinity）→ `—`，0 → `0`。
 *
 * `locale` 由调用方传入（页面上是当前界面语言）；不传则跟随运行环境。
 * 与 `lib/format.ts` 的 `fmtCredit` 是**同一实现** —— 那边只是补上 `intlLocale()`。
 */
export function creditText(v: number | null | undefined, locale?: string): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  if (v === 0) return '0';
  if (Math.abs(v) >= 100) return Math.round(v).toLocaleString(locale);
  if (Math.abs(v) >= 1) return v.toFixed(2);
  return v.toFixed(4);
}

/** 耗时格式化：不足 1 秒用毫秒，否则用秒（两位小数）。null = 未采集。 */
export function latencyText(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/**
 * `tool_repair` 的文案。
 *
 * null / 缺字段按 `none` 处理：老记录与「没开修复」在库里都是 none，
 * 显示「未修复」是准确的说法；未知值原样显示（上游将来加了新状态时，
 * 我们宁可让人看到原文，也不要渲染成空白）。
 */
export function toolRepairLabel(value: string | null | undefined, t: (key: string) => string): string {
  switch (value) {
    case undefined:
    case null:
    case '':
    case 'none':
      return t('logs.toolRepairNone');
    case 'fixed':
      return t('logs.toolRepairFixed');
    case 'failed':
      return t('logs.toolRepairFailed');
    case 'recalled':
      return t('logs.toolRepairRecalled');
    default:
      return String(value);
  }
}

/** `tool_repair` 的色调：修复成功是好事，失败是坏事，重调模型只是需留意。 */
export function toolRepairTone(value: string | null | undefined): LogTone {
  switch (value) {
    case 'fixed':
      return 'ok';
    case 'failed':
      return 'bad';
    case 'recalled':
      return 'warn';
    default:
      return 'neutral';
  }
}

/** 空值兜底：空串与 null / undefined 都显示破折号，不渲染成空白。 */
function orDash(value: string | null | undefined): string {
  return value ? value : '—';
}

/**
 * 组装抽屉的全部行。
 *
 * 顺序固定（网络 → 身份 → 模型 → 时序 → 计量 → 结果），不随字段有无变化：
 * 顺序一乱，用户对比两条日志时就得重新找行。缺的字段**仍然占一行**并显示
 * 「未采集 / —」，否则「这一项是空的」和「这一项不存在」看起来一样。
 */
export function logDetailRows(log: LogDetailSource, deps: LogDetailDeps): LogDetailRow[] {
  const {t} = deps;
  const n = deps.n ?? ((value: number) => String(value));
  const num = (value: number | null | undefined) =>
    value === null || value === undefined || !Number.isFinite(value) ? '—' : n(value);
  const collected = (value: number | null | undefined) =>
    value === null || value === undefined ? t('logs.notCollectedShort') : n(value);

  return [
    {id: 'ip', label: t('logs.rowIp'), value: orDash(log.ip), copyable: !!log.ip},
    {id: 'key', label: t('logs.rowKey'), value: orDash(log.key_name)},
    // 账号来自上游日志回填，可能比请求本身晚几秒；这里只能说「尚未采集」，
    // 不能写成「非流式请求」——那是在猜原因。
    {id: 'account', label: t('logs.rowAccount'), value: log.account || t('logs.accountPending')},
    // 版本：历史记录该列为 NULL，后端按国内版归类（与 db.realm_of_model 同口径）。
    {
      id: 'realm',
      label: t('logs.rowRealm'),
      value: log.realm === 'global' ? t('realm.global') : t('realm.cn'),
    },
    {id: 'model', label: t('logs.rowModel'), value: orDash(log.model)},
    {id: 'mapped', label: t('logs.rowMappedModel'), value: orDash(log.mapped_model)},
    {
      id: 'status',
      label: t('logs.rowStatus'),
      value: log.status === null || log.status === undefined ? '—' : String(log.status),
    },
    {
      id: 'firstToken',
      label: t('logs.rowFirstToken'),
      value:
        log.first_token_ms === null || log.first_token_ms === undefined
          ? t('logs.notCollected')
          : latencyText(log.first_token_ms),
    },
    {id: 'latency', label: t('logs.rowLatency'), value: latencyText(log.latency_ms)},
    {id: 'promptTokens', label: t('logs.rowPromptTokens'), value: num(log.prompt_tokens)},
    {id: 'completionTokens', label: t('logs.rowCompletionTokens'), value: num(log.completion_tokens)},
    // 提示词缓存三段。null 显示「未采集」而不是 0：老上游不返回这三个字段，
    // 显示 0 会让人以为缓存从未生效。
    {id: 'cacheHit', label: t('logs.rowCacheHit'), value: collected(log.cache_hit_tokens)},
    {id: 'cacheMiss', label: t('logs.rowCacheMiss'), value: collected(log.cache_miss_tokens)},
    {id: 'cacheWrite', label: t('logs.rowCacheWrite'), value: collected(log.cache_write_tokens)},
    {
      id: 'credit',
      label: t('logs.rowCredit'),
      value: creditText(log.credit, deps.locale),
      // 有扣费才标色：0 是「没扣」而不是「异常」，不该抢眼。
      tone: typeof log.credit === 'number' && log.credit > 0 ? 'warn' : 'neutral',
    },
    {
      id: 'stream',
      label: t('logs.rowStream'),
      value: log.stream ? t('common.yes') : t('common.no'),
    },
    {
      id: 'toolRepair',
      label: t('logs.rowToolRepair'),
      value: toolRepairLabel(log.tool_repair, t),
      tone: toolRepairTone(log.tool_repair),
    },
    // UA / error 常需要整段贴出去（排查 / 反馈）：可复制，且按长文本排版。
    {id: 'ua', label: t('logs.rowUa'), value: orDash(log.ua), copyable: !!log.ua, scroll: true},
    {id: 'error', label: t('logs.rowError'), value: orDash(log.error), copyable: !!log.error, scroll: true},
  ];
}
