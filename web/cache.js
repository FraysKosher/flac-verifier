// Result cache keyed by file identity + analysis options. IndexedDB in the
// browser; any {get, put, clear} backend can be injected (tests use memory).

export function cacheKey({ relPath, size, lastModified, mode, seconds, engineVersion }) {
  return JSON.stringify([relPath, size, lastModified, mode, mode === 'seconds' ? seconds : null, engineVersion]);
}

export function memoryBackend() {
  const map = new Map();
  return {
    async get(key) { return map.get(key); },
    async put(key, value) { map.set(key, value); },
    async clear() { map.clear(); },
  };
}

function idbBackend() {
  const STORE = 'results';
  const opened = new Promise((resolve, reject) => {
    const req = indexedDB.open('flac-verifier-cache', 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  async function tx(mode, fn) {
    const db = await opened;
    return new Promise((resolve, reject) => {
      const t = db.transaction(STORE, mode);
      const req = fn(t.objectStore(STORE));
      t.oncomplete = () => resolve(req.result);
      t.onerror = () => reject(t.error);
    });
  }
  return {
    get: (key) => tx('readonly', (s) => s.get(key)),
    put: (key, value) => tx('readwrite', (s) => s.put(value, key)),
    clear: () => tx('readwrite', (s) => s.clear()),
  };
}

export function openCache(backend) {
  const store = backend || idbBackend();
  return {
    get: (key) => store.get(key),
    put: (key, entry) => store.put(key, { json: entry.json, png: entry.png ?? null }),
    clear: () => store.clear(),
  };
}
