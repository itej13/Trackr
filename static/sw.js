// Keeps the app shell on the device so it opens with no network. API calls always go to the server.
const CACHE = 'timesheet-v1';
const SHELL = ['/', '/app.js', '/style.css', '/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener('activate', e => e.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));

// Prefer the server, so updates show at once, but fall back to the cached copy after 2 s: when the server
// is out of reach a request can hang for a minute before it fails.
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  e.respondWith(caches.open(CACHE).then(async cache => {
    const key = url.pathname === '/index.html' ? '/' : url.pathname;
    const fresh = fetch(e.request).then(r => { if (r.ok) cache.put(key, r.clone()); return r; });
    const cached = await cache.match(key);
    if (!cached) return fresh;
    return Promise.race([fresh.catch(() => cached), new Promise(done => setTimeout(() => done(cached), 2000))]);
  }));
});
