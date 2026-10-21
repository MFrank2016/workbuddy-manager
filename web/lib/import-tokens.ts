/**
 * 批量 refresh token 导入的**纯逻辑**（零依赖，Node 可直接跑 .ts）。
 *
 * 为什么从组件里拆出来：这里每一条规则都对应一种「界面上看起来成功了、
 * 实际却出了错」的事故，只有做成可单测的纯函数才守得住——
 *
 *   1. **非法 JSON 本地拦下**。放到后端去撞，用户得到的是一次 400 和一句
 *      「请求体必须是对象」，既不知道哪一行坏了，也白等一次往返。
 *   2. **200 条上限先查**。这是后端的硬上限（`tokenimport.MAX_ENTRIES`），
 *      先拦下来比让 200 条各失败一次好。
 *   3. **重复 uid 必须报出来**。后端按顺序各写一遍（并发 2），谁最后落盘
 *      不确定；用户看到「成功」，却不知道留下的是哪一份 token。这是本模块
 *      存在的最强理由。
 *   4. **逐条字段校验提前给出原因**（缺 uid / 缺 refreshToken / uid 形态非法），
 *      而不是等后端把这条记成 `rejected` 再回头猜。
 *
 * 输入形态认三种（都是用户真实会粘贴的东西）：
 *   · 完整 auth JSON 的数组，条目可以是 `{account, auth}` 嵌套形或扁平对象；
 *   · 单个对象，或 `{entries: [...]}` 包装；
 *   · 每行 `uid,refresh_token` 的表格粘贴文本（空行 / 首尾空白 / 引号都容忍）。
 *
 * 归一化后的条目**原样交给后端**：后端已经认得这两种形态，前端再拆一遍等于
 * 第二份事实来源（而且会把 `{account, auth}` 里那些昵称 / 域名 / device_token
 * 丢掉）。CSV 行没有这些字段，才在这里合成最小扁平对象。
 *
 * 不 import 任何东西（与 `retention-draft.ts` 同一条约束）：`web/lib/*.test.mjs`
 * 直接 import 本文件，引入运行时 import 会让测试以「找不到模块」失败。
 */

/** 与后端 `tokenimport.MAX_ENTRIES` 一致；改这里必须同步改后端。 */
export const MAX_ENTRIES = 200;

/** 与后端 `_UID_RE` 一致：uid 会拼进文件名，形态必须能当文件名用。 */
const UID_RE = /^[0-9A-Za-z_-]{1,80}$/;

/** 提交体里的 realm：空 = 由后端按条目自己的 realm / 域名推导。 */
export type ImportRealm = '' | 'cn' | 'global';

export interface ImportError {
  /** 在 `entries` 里的下标（从 0 开始），便于界面上定位 */
  index: number;
  /** 能取到的 uid（取不到就是空串） */
  uid: string;
  /** 与后端 `parse_entry` 的 reason 同名：missing_uid / invalid_uid / … */
  reason: 'invalid_entry' | 'missing_uid' | 'invalid_uid' | 'missing_refresh_token';
}

export interface ParsedImport {
  /** 阻断提交的原因；null = 可以提交（但仍要 `errors` / `duplicates` 为空） */
  fatal: 'empty' | 'invalid_json' | 'too_many' | null;
  /** 归一化后的条目，**原样**进请求体 */
  entries: unknown[];
  /** 与 `entries` 同序的 uid（取不到是空串），用于查重与结果对照 */
  uids: string[];
  /** 逐条字段问题 */
  errors: ImportError[];
  /** 出现不止一次的 uid（按首次出现顺序，每个只报一次） */
  duplicates: string[];
  /** 解析出的条目总数（含被字段校验拦下的那些），用于「超 200」提示 */
  count: number;
}

export interface ImportTarget {
  realm: ImportRealm;
  /** null / undefined = 默认分组（后端省略 `upstream_id`） */
  upstreamId?: number | null;
  overwrite: boolean;
}

export interface ImportPayload {
  realm: ImportRealm;
  entries: unknown[];
  overwrite: boolean;
  upstream_id?: number;
}

/** 去首尾空白并把 undefined / null 归一成空串。 */
function text(value: unknown): string {
  return value === undefined || value === null ? '' : String(value).trim();
}

/** 取第一个非空值（嵌套优先的调用方按优先级传参，与后端 `_first` 同义）。 */
function first(...values: unknown[]): string {
  for (const value of values) {
    const got = text(value);
    if (got) return got;
  }
  return '';
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/**
 * 从一条（任意形态的）条目里取 uid：`account.uid > account.user_id > uid > user_id`。
 *
 * 顺序与后端 `parse_entry` 逐字一致——前端算出的重复 uid 必须和后端认定的
 * 是同一个，否则「前端说没重复、后端却写到同一个文件」。
 */
function uidOf(entry: unknown): string {
  const raw = asRecord(entry);
  const account = asRecord(raw.account);
  return first(account.uid, account.user_id, raw.uid, raw.user_id);
}

/** refresh token 是否存在：嵌套优先，其次扁平，`refresh_token` / `refreshToken` 都认。 */
function refreshOf(entry: unknown): string {
  const raw = asRecord(entry);
  const auth = asRecord(raw.auth);
  return first(auth.refreshToken, auth.refresh_token,
    raw.refreshToken, raw.refresh_token);
}

function isRecord(value: unknown): boolean {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** 去掉表格粘贴常见的成对引号（Excel / 表格软件复制出来会带）。 */
function unquote(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length >= 2
      && ((trimmed.startsWith('"') && trimmed.endsWith('"'))
        || (trimmed.startsWith("'") && trimmed.endsWith("'")))) {
    return trimmed.slice(1, -1).trim();
  }
  return trimmed;
}

/**
 * 每行 `uid,refresh_token` → 最小扁平条目。
 *
 * 只按**第一个**逗号切分：token 里若带逗号（粘贴片段、非标准串），整段都得留着，
 * 否则被截断的 token 会在后端表现为「刷新失败」——一个查不出原因的假失败。
 */
function parseLine(line: string): unknown {
  const cut = line.indexOf(',');
  if (cut < 0) return line;
  const uid = unquote(line.slice(0, cut));
  const refresh = unquote(line.slice(cut + 1));
  return {uid, refresh_token: refresh};
}

function emptyResult(fatal: ParsedImport['fatal']): ParsedImport {
  return {fatal, entries: [], uids: [], errors: [], duplicates: [], count: 0};
}

/** 逐条字段校验；返回问题清单（顺序与 entries 一致）。 */
function validate(entries: unknown[]): ImportError[] {
  const errors: ImportError[] = [];
  entries.forEach((entry, index) => {
    if (!isRecord(entry)) {
      errors.push({index, uid: '', reason: 'invalid_entry'});
      return;
    }
    const uid = uidOf(entry);
    if (!uid) {
      errors.push({index, uid: '', reason: 'missing_uid'});
      return;
    }
    if (!UID_RE.test(uid)) {
      errors.push({index, uid, reason: 'invalid_uid'});
      return;
    }
    if (!refreshOf(entry)) {
      errors.push({index, uid, reason: 'missing_refresh_token'});
    }
  });
  return errors;
}

/**
 * 解析粘贴的文本。
 *
 * 判定顺序：空 → JSON（首字符 `[` / `{`）→ 否则按 CSV 行处理。
 * **不猜**：首字符是 `[` / `{` 却解析失败，就是 `invalid_json`，不会再退化成
 * 「每行一条」去把 `[{"uid":...` 当成 uid 拼出来。
 */
export function parseImportText(input: string): ParsedImport {
  // BOM 来自 Windows 记事本 / 表格导出，`JSON.parse` 见到它会直接报错。
  const source = input.replace(/^\uFEFF/, '');
  const trimmed = source.trim();
  if (!trimmed) return emptyResult('empty');

  let entries: unknown[];
  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return emptyResult('invalid_json');
    }
    if (Array.isArray(parsed)) {
      entries = parsed;
    } else if (isRecord(parsed) && Array.isArray((parsed as {entries?: unknown}).entries)) {
      entries = (parsed as {entries: unknown[]}).entries;
    } else if (isRecord(parsed)) {
      entries = [parsed];
    } else {
      // 纯数字 / 字符串 / 布尔 / null：不是条目，也不是「每行一条」的输入。
      return emptyResult('invalid_json');
    }
  } else {
    entries = source
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line.length > 0)
      .map(parseLine);
  }

  if (!entries.length) return emptyResult('empty');
  if (entries.length > MAX_ENTRIES) {
    return {...emptyResult('too_many'), count: entries.length};
  }

  const uids = entries.map(uidOf);
  const seen = new Set<string>();
  const duplicates: string[] = [];
  for (const uid of uids) {
    if (!uid) continue;
    if (seen.has(uid)) {
      if (!duplicates.includes(uid)) duplicates.push(uid);
    } else {
      seen.add(uid);
    }
  }

  return {
    fatal: null,
    entries,
    uids,
    errors: validate(entries),
    duplicates,
    count: entries.length,
  };
}

/** 是否具备「点提交」的条件：没有阻断原因、没有字段问题、没有重复。 */
export function canSubmit(parsed: ParsedImport): boolean {
  return parsed.fatal === null
    && parsed.entries.length > 0
    && parsed.errors.length === 0
    && parsed.duplicates.length === 0;
}

/**
 * 组装请求体。
 *
 * `upstream_id` **只在选定具体分组时才出现**：默认分组的定义在后端
 * （`upstreamsvc.default_upstream()`），前端传 `null` 或 `0` 都是在替它做决定。
 */
export function buildImportPayload(parsed: ParsedImport, target: ImportTarget): ImportPayload {
  const body: ImportPayload = {
    realm: target.realm,
    entries: parsed.entries,
    overwrite: target.overwrite,
  };
  if (target.upstreamId !== undefined && target.upstreamId !== null) {
    body.upstream_id = target.upstreamId;
  }
  return body;
}
