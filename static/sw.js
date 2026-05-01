const CACHE_VERSION = 'pool-v2';
const SHELL = [
  '/static/style.css',
  '/static/img/favicon.png',
];

// Default skip list — overwritten at activate time by /api/sw-skip-list
let skipPrefixes = ['/add', '/edit', '/delete', '/api/', '/backup', '/treatments',
                    '/products', '/regenerate', '/test-telegram', '/test-ha-push',
                    '/settings', '/sw.js'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    fetch('/api/sw-skip-list')
      .then(r => r.json())
      .then(data => { if (Array.isArray(data.skip)) skipPrefixes = data.skip; })
      .catch(() => {}) // keep defaults on network failure
      .then(() => caches.keys())
      .then(ks => Promise.all(ks.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;

  // Mutations / API : toujours réseau, pas de cache
  if (skipPrefixes.some(p => url.pathname.startsWith(p))) return;

  // Assets statiques locaux + CDN : stale-while-revalidate
  if (url.pathname.startsWith('/static/') || url.hostname !== location.hostname) {
    e.respondWith(
      caches.open(CACHE_VERSION).then(cache =>
        cache.match(e.request).then(cached => {
          const fetchPromise = fetch(e.request).then(resp => {
            if (resp.ok) cache.put(e.request, resp.clone());
            return resp;
          });
          return cached || fetchPromise;
        })
      )
    );
    return;
  }

  // Pages HTML : réseau d'abord, cache en fallback (mode hors-ligne)
  e.respondWith(
    fetch(e.request)
      .then(resp => {
        const clone = resp.clone();
        caches.open(CACHE_VERSION).then(c => c.put(e.request, clone));
        return resp;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match('/')))
  );
});
