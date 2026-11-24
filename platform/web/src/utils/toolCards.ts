export interface ToolCardState {
  id?: string;
  tool: string;
  ok: boolean;
  result: string;
  status?: string;
  approved?: boolean;
  auto?: boolean;
  rejected?: boolean;
  args?: any;
}

const FINAL_STATES = new Set(["completed", "error", "cancelled", "interrupted"]);

export function toolCallKey(id?: string) {
  if (!id) return undefined;
  const separator = id.indexOf(":");
  return separator >= 0 ? id.slice(separator + 1) : id;
}

export function upsertToolCard(cards: ToolCardState[], card: ToolCardState) {
  const key = toolCallKey(card.id);
  const index = key ? cards.findIndex((item) => toolCallKey(item.id) === key) : -1;
  if (index < 0) return [...cards, {...card, id: key}];
  const next = cards.slice();
  next[index] = {...next[index], ...card, id: key, args: card.args ?? next[index].args};
  return next;
}

function recordStatus(status: string) {
  return ({pending: "awaiting_approval", approved: "running", executed: "completed",
    rejected: "cancelled"} as Record<string, string>)[status] ?? status;
}

export function projectToolCards(events: any[], records: any[] = []) {
  let cards: ToolCardState[] = [];
  for (const event of events) {
    if (event.type !== "tool_state" && event.type !== "tool_result") continue;
    const payload = event.payload ?? {};
    const isResult = event.type === "tool_result";
    cards = upsertToolCard(cards, {
      id: payload.tool_call_id,
      tool: payload.tool ?? "Tool",
      ok: isResult && Boolean(payload.ok),
      result: payload.result ?? "",
      status: payload.status ?? (isResult
        ? payload.rejected ? "cancelled" : payload.ok ? "completed" : "error"
        : "queued"),
      approved: payload.approved,
      auto: payload.auto,
      rejected: payload.rejected,
      args: payload.args,
    });
  }
  for (const record of records) {
    const id = toolCallKey(record.id);
    const status = recordStatus(record.status ?? "queued");
    const existing = cards.find((card) => toolCallKey(card.id) === id);
    if (existing?.status && FINAL_STATES.has(existing.status)) {
      if (existing.args == null && record.args != null) {
        cards = upsertToolCard(cards, { ...existing, args: record.args });
      }
      continue;
    }
    cards = upsertToolCard(cards, {
      id, tool: record.tool, ok: status === "completed", result: record.result ?? "",
      status, rejected: status === "cancelled" || undefined, args: record.args ?? {},
    });
  }
  return cards;
}
