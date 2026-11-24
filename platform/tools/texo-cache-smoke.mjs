import assert from "node:assert/strict";
import { TexoReceiptStore } from "../web/src/utils/texoCache.ts";

class MemoryStorage {
  values = new Map();
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, value); }
  removeItem(key) { this.values.delete(key); }
}
class MemoryCacheStorage {
  values = new Map();
  async open() {
    return {
      match: async key => this.values.has(key) ? new Response(this.values.get(key)) : undefined,
      put: async (key, response) => { this.values.set(key, await response.text()); },
      delete: async key => this.values.delete(key),
    };
  }
}
const storage = new MemoryStorage();
const cache = new MemoryCacheStorage();
const store = new TexoReceiptStore(1, storage, cache);
const a = await store.hash("image-A");
const b = await store.hash("image-A");
const c = await store.hash("image-B");
assert.equal(a, b);
assert.notEqual(a, c);
const key = "vf_texo_receipts_v2_1";
const url = "/texo/receipts/v2/1";
const history = Array.from({length:25}, (_, i) => ({
  hash:(i+1).toString(16).padStart(64,"0"), latex:"x", model:"FormulaNet", created_at:Date.now()-i-10,
}));
storage.setItem(key, JSON.stringify(history));
await store.prune();
assert.equal(storage.getItem(key), JSON.stringify(history.slice(0,20)));
assert.equal(cache.values.get(url), storage.getItem(key));
await Promise.all([store.save(a, "E=mc^2"), store.save(c, "x=1")]);
const reader = new TexoReceiptStore(1, storage, cache);
assert.equal((await reader.load(a)).latex, "E=mc^2");
assert.equal((await reader.load(c)).latex, "x=1");
assert.equal(await reader.load(history.at(-1).hash), null);
assert.equal(await new TexoReceiptStore(2, storage, cache).load(a), null);
storage.setItem(key, '{ invalid');
await new TexoReceiptStore(1, storage, cache).prune();
assert.equal(storage.getItem(key), cache.values.get(url));
console.log("Texo receipt integrity, isolation, mirror recovery and retention: OK");
