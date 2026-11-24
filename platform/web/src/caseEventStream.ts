import { api, ApiError } from "./api";

interface CaseEvent {
  case_id: number; event_id: string; seq: number; type: string;
  paths_complete?: boolean; paths?: string[];
}
interface EventPage {
  case_id: number; events: CaseEvent[]; next_after: number; watermark: number;
  has_more: boolean; blocked: boolean;
}

export interface CaseTreeUpdate {
  full: boolean;
  initial: boolean;
  paths: string[];
  through?: number;
}

const HINT_DELAY = 75;
const POLL_INTERVAL = 5000;
const SNAPSHOT_INTERVAL = 60000;

/** Ordered database pages supply paths and advance the cursor only after a successful refresh. */
export function watchCaseEvents(caseId: number, reconcile: (update: CaseTreeUpdate) => Promise<number | null>) {
  let stopped = false, cursor = 0, busy = false, requested = false;
  let fullNeeded = true, initial = true, coveredThrough = 0, epoch = 0, lastSnapshot = 0;
  let socket: WebSocket | undefined;
  let scheduled: ReturnType<typeof setTimeout> | undefined;
  let reconnect: ReturnType<typeof setTimeout> | undefined;
  let delay = 500;

  const apply = async (full: boolean, paths: string[], useInitial: boolean, through?: number) => {
    const watermark = await reconcile({full, paths, initial: useInitial, through});
    if (watermark !== null) coveredThrough = watermark;
    if (full || watermark !== null) lastSnapshot = Date.now();
  };

  const catchUp = async () => {
    scheduled = undefined;
    if (busy || stopped) return;
    busy = true;
    try {
      while (requested && !stopped) {
        requested = false;
        const requestEpoch = epoch;
        let full = fullNeeded;
        fullNeeded = false;
        const batch = await api.get<EventPage>(`/cases/${caseId}/workflow-events?after=${cursor}&limit=100`);
        if (stopped) return;
        if (requestEpoch !== epoch) { requested = true; continue; }
        let next = cursor;
        const ids = new Set<string>(), paths = new Set<string>();
        if (batch.case_id !== caseId || !Array.isArray(batch.events)
            || typeof batch.has_more !== 'boolean' || typeof batch.blocked !== 'boolean') throw new Error("Invalid event page");
        for (const event of batch.events) {
          if (event.case_id !== caseId || !Number.isSafeInteger(event.seq) || event.seq <= next
              || typeof event.event_id !== "string" || ids.has(event.event_id)) throw new Error("Invalid event order");
          if (event.seq > coveredThrough && event.seq !== next + 1) full = true;
          next = event.seq; ids.add(event.event_id);
          if (event.seq <= coveredThrough || event.type !== 'files.changed') continue;
          if (event.paths_complete !== true || !Array.isArray(event.paths) || !event.paths.length
              || event.paths.some(path => typeof path !== 'string' || !path)) {
            full = true;
          } else {
            for (const path of event.paths) paths.add(path);
          }
        }
        if (batch.next_after !== next || !Number.isSafeInteger(batch.watermark) || batch.watermark < next
            || (batch.has_more && next === cursor)) throw new Error("Invalid event cursor");
        if (batch.blocked && batch.watermark > coveredThrough) full = true;
        if (paths.size > 256 || new TextEncoder().encode([...paths].join('')).length > 8192) full = true;
        if (full || batch.events.length) await apply(full, full ? [] : [...paths], initial, batch.watermark);
        if (stopped) return;
        if (requestEpoch !== epoch) { requested = true; coveredThrough = 0; lastSnapshot = 0; continue; }
        cursor = next;
        initial = false;
        if (batch.has_more) requested = true;
      }
    } catch (error) {
      if (stopped) return;
      if (error instanceof ApiError && error.code === "event_cursor_ahead") {
        reset();
      } else {
        // Keep the cursor on a failed event or patch request. The current tree
        // can still be read when the event API is degraded.
        try {
          await apply(true, [], false);
        } catch {
          fullNeeded = true;
        }
        initial = false;
      }
    } finally {
      busy = false;
      if (requested && !stopped && scheduled === undefined) scheduled = setTimeout(catchUp, HINT_DELAY);
    }
  };

  const request = (full = false) => {
    if (stopped) return;
    requested = true;
    fullNeeded ||= full;
    if (!busy && scheduled === undefined) scheduled = setTimeout(catchUp, HINT_DELAY);
  };

  function reset() {
    cursor = 0; coveredThrough = 0; lastSnapshot = 0; initial = false; ++epoch;
    request(true);
  }

  const reconnectLater = () => {
    if (!stopped) { reconnect = setTimeout(() => void connect(), delay); delay = Math.min(delay * 2, 10000); }
  };

  const connect = async () => {
    if (stopped) return;
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    let ticket: string;
    try {
      ({ ticket } = await api.post<{ ticket: string }>("/connection-tickets", { scope: "events", case_id: caseId }));
    } catch (error) {
      if (!(error instanceof ApiError && [401, 403, 404].includes(error.status))) reconnectLater();
      return;
    }
    if (stopped) return;
    const connection = new WebSocket(`${protocol}://${location.host}/ws/events?case_id=${caseId}&after=${cursor}&token=${encodeURIComponent(ticket)}`);
    socket = connection;
    connection.onopen = () => { if (!stopped && socket === connection) { delay = 500; request(); } };
    connection.onmessage = (message) => {
      if (stopped || socket !== connection) return;
      try {
        const event = JSON.parse(message.data);
        if (event.case_id !== caseId) return;
        if (event.type === "cursor.reset") { reset(); return; }
        request(event.type === "files.changed" && !event.event_id);
      } catch { /* Malformed hints cannot change application state. */ }
    };
    connection.onclose = () => {
      if (!stopped && socket === connection) reconnectLater();
    };
    connection.onerror = () => connection.close();
  };

  const poll = setInterval(() => request(socket?.readyState !== WebSocket.OPEN ||
    Date.now() - lastSnapshot >= SNAPSHOT_INTERVAL), POLL_INTERVAL);
  const online = () => request();
  window.addEventListener("online", online);
  request(); void connect();
  return () => {
    stopped = true; clearInterval(poll); clearTimeout(scheduled); clearTimeout(reconnect);
    window.removeEventListener("online", online); socket?.close();
  };
}
