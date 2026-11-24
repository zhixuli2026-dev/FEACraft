export interface TexoReceipt {
  hash: string;
  latex: string;
  created_at: number;
  model: string;
}

const LEGACY_PREFIX = "vf_texo_receipt_";
const LEGACY_CACHE = "vf-texo-receipts-v1";
const CACHE_NAME = "vf-texo-receipts-v2";
const MAX_RECEIPTS = 20;
const MAX_LATEX = 8192;
const MAX_SNAPSHOT = MAX_RECEIPTS * (MAX_LATEX * 6 + 256);
const HASH = /^[0-9a-f]{64}$/;

let pending = Promise.resolve();

function optional<T>(action: () => T): T | undefined {
  try { return action(); } catch { return undefined; }
}

async function storageOperation<T>(action: () => Promise<T>): Promise<T | undefined> {
  try { return await action(); } catch { return undefined; }
}

function serialized<T>(action: () => Promise<T>): Promise<T> {
  const result = pending.then(async () => {
    const locks = optional(() => globalThis.navigator?.locks);
    let started = false;
    if (locks) {
      try {
        return await locks.request("vf-texo-receipts", () => {
          started = true;
          return action();
        });
      } catch (error) {
        if (started) throw error;
      }
    }
    return action();
  });
  pending = result.then(() => undefined, () => undefined);
  return result;
}

function receipt(value: unknown, hash?: string): value is TexoReceipt {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<TexoReceipt>;
  return Object.keys(item).length === 4 && typeof item.hash === "string" && HASH.test(item.hash) && (!hash || item.hash === hash) &&
    item.model === "FormulaNet" && typeof item.latex === "string" && item.latex.trim().length > 0 &&
    item.latex.length <= MAX_LATEX && Number.isSafeInteger(item.created_at) &&
    Number(item.created_at) > 0 && Number(item.created_at) <= Date.now() + 60000;
}

function parse(raw: string | null | undefined): unknown {
  return raw && raw.length <= MAX_SNAPSHOT ? optional(() => JSON.parse(raw)) : undefined;
}

function latest(values: TexoReceipt[]): TexoReceipt[] {
  const sorted = [...values].sort((a, b) => b.created_at - a.created_at || a.hash.localeCompare(b.hash));
  const unique = new Map<string, TexoReceipt>();
  for (const item of sorted) {
    if (!unique.has(item.hash)) unique.set(item.hash, item);
    if (unique.size === MAX_RECEIPTS) break;
  }
  return [...unique.values()];
}

export class TexoReceiptStore {
  private readonly key: string;
  private readonly url: string;
  private readonly storage: Storage | undefined;
  private readonly caches: CacheStorage | undefined;
  private memory: TexoReceipt[] = [];

  constructor(accountId: number,
              storage = optional(() => globalThis.localStorage),
              caches = optional(() => globalThis.caches)) {
    this.key = `vf_texo_receipts_v2_${accountId}`;
    this.url = `/texo/receipts/v2/${accountId}`;
    this.storage = storage;
    this.caches = caches;
  }

  async hash(dataUrl: string): Promise<string | null> {
    const bytes = new TextEncoder().encode(dataUrl);
    const digest = await storageOperation(() => crypto.subtle.digest("SHA-256", bytes));
    return digest ? Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, "0")).join("") : null;
  }

  private cache(name: string): Promise<Cache | undefined> {
    return this.caches ? storageOperation(() => this.caches!.open(name)) : Promise.resolve(undefined);
  }

  private async read(cache: Cache | undefined): Promise<TexoReceipt[]> {
    const local = parse(optional(() => this.storage?.getItem(this.key)));
    const remote = parse(await storageOperation(async () => (await cache?.match(this.url))?.text()));
    return latest([...this.memory, ...[local, remote].flatMap(value => Array.isArray(value) ? value.filter(item => receipt(item)) : [])]);
  }

  private async write(cache: Cache | undefined, entries: TexoReceipt[]): Promise<boolean> {
    this.memory = entries;
    const json = JSON.stringify(entries);
    const local = this.storage && optional(() => {
      this.storage!.setItem(this.key, json);
      return true;
    });
    const remote = cache && await storageOperation(async () => {
      await cache.put(this.url, new Response(json, {headers: {"Content-Type": "application/json"}}));
      return true;
    });
    return Boolean(local || remote);
  }

  async load(hash: string): Promise<TexoReceipt | null> {
    if (!HASH.test(hash)) return null;
    return serialized(async () => {
      const cache = await this.cache(CACHE_NAME);
      const entries = await this.read(cache);
      const found = entries.find(item => item.hash === hash);
      if (found) return found;

      // A legacy result can be claimed only by selecting its original image.
      const legacyCache = await this.cache(LEGACY_CACHE);
      const legacyKey = LEGACY_PREFIX + hash;
      const legacyUrl = `/receipts/${hash}`;
      const local = parse(optional(() => this.storage?.getItem(legacyKey)));
      const remote = receipt(local, hash) ? undefined
        : parse(await storageOperation(async () => (await legacyCache?.match(legacyUrl))?.text()));
      const legacy = receipt(local, hash) ? local : receipt(remote, hash) ? remote : null;
      if (legacy && await this.write(cache, latest([{...legacy, created_at: Date.now()}, ...entries]))) {
        optional(() => this.storage?.removeItem(legacyKey));
        await storageOperation(async () => legacyCache?.delete(legacyUrl));
      }
      return legacy;
    });
  }

  async save(hash: string, latex: string): Promise<void> {
    const item = {hash, latex, created_at: Date.now(), model: "FormulaNet"};
    if (!receipt(item)) return;
    await serialized(async () => {
      const cache = await this.cache(CACHE_NAME);
      await this.write(cache, latest([item, ...await this.read(cache)]));
    });
  }

  async prune(): Promise<void> {
    await serialized(async () => {
      const cache = await this.cache(CACHE_NAME);
      const entries = await this.read(cache);
      if (entries.length || optional(() => this.storage?.getItem(this.key)) ||
          await storageOperation(async () => cache?.match(this.url))) await this.write(cache, entries);

      const indexKey = LEGACY_PREFIX + "index";
      const oldIndex = parse(optional(() => this.storage?.getItem(indexKey)));
      if (!Array.isArray(oldIndex)) return;
      const hashes = [...new Set(oldIndex.filter((hash): hash is string => typeof hash === "string" && HASH.test(hash)))];
      if (hashes.length <= MAX_RECEIPTS && hashes.length === oldIndex.length) return;
      const legacy = await this.cache(LEGACY_CACHE);
      for (const hash of hashes.slice(MAX_RECEIPTS)) {
        optional(() => this.storage?.removeItem(LEGACY_PREFIX + hash));
        await storageOperation(async () => legacy?.delete(`/receipts/${hash}`));
      }
      optional(() => this.storage?.setItem(indexKey, JSON.stringify(hashes.slice(0, MAX_RECEIPTS))));
    });
  }
}
