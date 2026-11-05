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
