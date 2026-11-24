import type { NativeUsageValue } from '../panels/NativeUsage';

export interface NativeTurn {
  id: string; session_id: string; client_message_id: string; phase: string; status: string;
  last_seq: number; runtime?: string; cleanup_verified: boolean | null; error?: string;
  created_at: string; mode: string; usage?: NativeUsageValue;
}
export interface NativeEvent { type: string; turn_id: string; seq: number; payload: Record<string, any> }
export const isTerminal = (phase: string) => ['completed', 'failed', 'cancelled', 'interrupted'].includes(phase);
export const phaseLabel = (phase: string) => ({
  idle: '就绪', admitted: '已接收', forking: '正在创建原生分支', registered: '分支已登记，正在收尾', starting: '正在启动', running: '执行中', waiting_approval: '等待审批',
  awaiting_input: '等待你的回应', stopping: '正在停止', finalizing: '正在收尾', completed: '已完成', failed: '失败', cancelled: '已取消',
  interrupted: '已中断', recovery_required: '等待运行恢复', evidence_invalid: '运行记录校验失败',
  waiting_continue: '等待继续', active: '执行中',
} as Record<string, string>)[phase] ?? phase;

/** Observer only. Reconnect uses the last accepted durable sequence, never POST. */
export async function readNativeEvents(response: Response, accept: (event: NativeEvent) => void) {
  if (!response.body) throw new Error('事件流不可用');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const dispatch = (block: string) => {
    const data = block.split('\n').filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).replace(/^ /, '')).join('\n');
    if (data) accept(JSON.parse(data));
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      buffer = buffer.replace(/\r\n/g, '\n');
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        dispatch(buffer.slice(0, boundary)); buffer = buffer.slice(boundary + 2);
      }
      if (buffer.length > 4 * 1024 * 1024) throw new Error('事件超过显示限制');
      if (done) break;
    }
    // An incomplete event at EOF is not committed locally; GET will replay it.
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

export function acceptNativeEvent(events: NativeEvent[], event: NativeEvent, turn: string): NativeEvent[] {
  if (event.type === 'execution.unavailable') throw new Error('运行需要恢复；请检查服务端记录');
  if (event.turn_id !== turn || !Number.isInteger(event.seq) || event.seq < 1) throw new Error('事件归属或序号无效');
  const last = events.at(-1)?.seq ?? 0;
  if (event.seq <= last) return events;
  if (event.seq !== last + 1) throw new Error('事件序号不连续，等待重新连接');
  return [...events, event];
}
