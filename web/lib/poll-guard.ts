/** 加账号轮询的代次与一次性交付守卫。纯函数，不依赖 React/DOM，便于 node:test 直接跑。 */
export interface PollGuard {
  begin(): number;
  invalidate(): number;
  isStale(generation: number): boolean;
  markDone(): void;
  isDone(): boolean;
}

export function createPollGuard(): PollGuard {
  let generation = 0;
  let done = false;
  return {
    begin: () => ++generation,
    invalidate: () => ++generation,
    isStale: (g) => g !== generation,
    markDone: () => {
      done = true;
    },
    isDone: () => done,
  };
}

export type TerminalAction = "ignore" | "announce" | "quiet";

/**
 * 决定一次 poll 终态该怎么处理：
 * - announce：正常成功，弹 toast、关弹窗、刷列表；
 * - quiet  ：服务端说这份结果已被本客户端消费过（重复轮询），静默收尾，不弹 toast；
 * - ignore ：本轮已完成 / 非终态，什么都不做。
 */
export function classifyTerminal(
  status: string,
  delivered: boolean,
): TerminalAction {
  if (delivered || status === "waiting") return "ignore";
  if (status === "already_handled") return "quiet";
  if (status === "success") return "announce";
  return "ignore";
}


/**
 * 造一个「浏览器实例 id」读取器。抽成纯函数是为了能直接测：
 * 隐私模式 / 企业策略下 sessionStorage.getItem 会抛 SecurityError，
 * 旧实现里这个异常会冒泡到轮询的 catch，连续 3 次后整个加账号流程
 * 变成 error —— 即使服务端已经拿到结果（终审 #5）。
 *
 * storage 为 null 表示没有 window（SSR / 静态导出构建期），返回空串；
 * 读写抛错时退回模块内的内存 id，保证同一页面每次拿到的是同一个值。
 */
export function createClientId(
  getStorage: () => Pick<Storage, 'getItem' | 'setItem'> | null,
  newId: () => string,
): () => string {
  const KEY = 'wb.add-account.client';
  let memId = '';
  return () => {
    // **惰性读**：静态导出时模块在构建期就被求值，那会儿没有 window；
    // 若在这里之外先把 storage 捕获成常量，浏览器里会永远拿到 null。
    let storage: Pick<Storage, 'getItem' | 'setItem'> | null;
    try {
      storage = getStorage();
    } catch {
      storage = null;
    }
    if (!storage) return '';
    try {
      const id = storage.getItem(KEY) || '';
      if (id) return id;
      const fresh = memId || newId();
      memId = fresh;
      storage.setItem(KEY, fresh);
      return fresh;
    } catch {
      if (!memId) memId = newId();
      return memId;
    }
  };
}
