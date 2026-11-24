export function isCjkPunctuation(text: string): boolean {
  return /^[\u3000-\u303f\uff00-\uffef]+$/u.test(text) && /^[\p{P}\p{S}\u3000]+$/u.test(text);
}

type PunctuationEvent = "beforeinput" | "compositionend";

/** Pair the two fallback events for one punctuation commit. */
export class ImePunctuationGate {
  private last: { event: PunctuationEvent; text: string; at: number } | null = null;

  handle(event: PunctuationEvent, text: string, now: number, send: (value: string) => void): boolean {
    if (!isCjkPunctuation(text)) {
      this.clear();
      return false;
    }
    const last = this.last;
    if (last && last.event !== event && last.text === text && now - last.at < 80) {
      this.clear();
      return true;
    }
    this.last = { event, text, at: now };
    send(text);
    return true;
  }

  clear() { this.last = null; }
}
