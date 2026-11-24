export const MAX_QUEUED_OUTPUT_BYTES = 2 * 1024 * 1024;
export const OUTPUT_CHUNK_BYTES = 64 * 1024;

/** Retain the newest bytes in a fixed buffer while xterm is busy. */
export class TerminalOutputQueue {
  private buffer: Uint8Array | null = null;
  private start = 0;
  private size = 0;
  private truncated = false;

  push(data: Uint8Array) {
    if (!data.length) return;
    const capacity = MAX_QUEUED_OUTPUT_BYTES;
    const buffer = this.buffer ??= new Uint8Array(capacity);
    const tail = data.subarray(Math.max(0, data.length - capacity));
    const discard = Math.max(0, this.size + tail.length - capacity);
    this.truncated ||= discard > 0 || tail.length < data.length;
    this.start = (this.start + discard) % capacity;
    this.size -= discard;
    const end = (this.start + this.size) % capacity;
    const first = Math.min(tail.length, capacity - end);
    buffer.set(tail.subarray(0, first), end);
    buffer.set(tail.subarray(first), 0);
    this.size += tail.length;
  }

  nextChunk() {
    const buffer = this.buffer;
    if (!buffer || !this.size) return null;
    const size = Math.min(this.size, OUTPUT_CHUNK_BYTES);
    const bytes = new Uint8Array(size);
    const first = Math.min(size, buffer.length - this.start);
    bytes.set(buffer.subarray(this.start, this.start + first));
    bytes.set(buffer.subarray(0, size - first), first);
    this.start = (this.start + size) % buffer.length;
    this.size -= size;
    const truncated = this.truncated;
    this.truncated = false;
    return { bytes, truncated };
  }

  clear() {
    this.buffer = null;
    this.start = this.size = 0;
    this.truncated = false;
  }

  get queuedBytes() { return this.size; }
}

/** 浅色终端兼容：仅重写黑色/亮黑背景，前景和其他 SGR 不变。 */
export function rewriteLightSgr(text: string): string {
  return text.replace(/\x1b\[([0-9;:]*)m/g, (_whole, codes: string) => {
    const parameters = codes.split(";");
    const rewritten: string[] = [];
    for (let index = 0; index < parameters.length; index++) {
      const code = Number(parameters[index]);
      if (code === 38 || code === 48 || code === 58) {
        const mode = Number(parameters[index + 1]);
        const length = mode === 2 ? 5 : mode === 5 ? 3 : 1;
        rewritten.push(...parameters.slice(index, index + length));
        index += length - 1;
      } else if (code === 40) {
        rewritten.push("48;2;241;245;249");
      } else if (code === 100) {
        rewritten.push("48;2;226;232;240");
      } else {
        rewritten.push(parameters[index]);
      }
    }
    return `\x1b[${rewritten.join(";")}m`;
  });
}

/** Hold a partial SGR sequence across WebSocket and xterm write boundaries. */
export class TerminalSgrStream {
  private pending = "";

  decode(text: string, light: boolean): string {
    const complete = this.pending + text;
    this.pending = complete.slice(-256).match(/\x1b(?:\[[0-9;:]*)?$/)?.[0] ?? "";
    const visible = this.pending ? complete.slice(0, -this.pending.length) : complete;
    return light ? rewriteLightSgr(visible) : visible;
  }

  clear() { this.pending = ""; }
}
