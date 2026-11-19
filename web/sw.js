// Cache worker: engine assets are cached on first use; the site itself is
// network-first so deploys show up immediately.
import { PYODIDE_INDEX_URL } from './runtime.js';

// assemble.sh replaces the placeholder with a hash of vendor/.
const VENDOR_REV = '__VENDOR_HASH__';
const CACHE = `fv-engine-${VENDOR_REV}-${PYODIDE_INDEX_URL}`;
const PREFIX = 'fv-engine-';
const inflight = new Map();

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith(PREFIX) && key !== CACHE) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

const cacheable = (res) => res.status === 200 && res.type !== 'opaque' && res.type !== 'error';

async function cacheFirst(request) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(request.url);
  if (hit) return hit;
  let pending = inflight.get(request.url);
  if (!pending) {
    pending = (async () => {
      const res = await fetch(request.url, { mode: 'cors', credentials: 'omit' });
      if (!cacheable(res)) return () => res.clone();
      await cache.put(request.url, res);
      // The body now lives in the cache; each caller reads its own copy.
      return async () => (await cache.match(request.url)) ?? fetch(request.url);
    })().finally(() => inflight.delete(request.url));
    inflight.set(request.url, pending);
  }
  return (await pending)();
}

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const res = await fetch(new Request(request, { cache: 'no-cache' }));
    if (cacheable(res)) await cache.put(request.url, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(request.url);
    if (hit) return hit;
    throw err;
  }
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET' || request.headers.has('range')) return;
  const url = new URL(request.url);
  if (request.url.startsWith(PYODIDE_INDEX_URL)) {
    event.respondWith(cacheFirst(request));
  } else if (url.origin === self.location.origin) {
    const vendor = url.pathname.startsWith(new URL('./vendor/', self.location.href).pathname);
    event.respondWith(vendor ? cacheFirst(request) : networkFirst(request));
  }
});
