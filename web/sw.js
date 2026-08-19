/* Corexia Service Worker v2 — enxuto (perf mobile).
   - NUNCA intercepta /api/, metodos != GET, cross-origin, /camthumb/ (snapshots ao vivo) nem URLs com ?query.
   - Navegacao: network-first -> index cacheado offline.
   - Assets com hash (/assets/, imagens/css/js): cache-first SEM revalidar em bg (imutaveis; nao re-baixa a cada load). */
var CACHE = 'corexia-v2';
var CORE = ['/', '/index.html', '/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', function (e) {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(CORE).catch(function () {}); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil((async function () {
    var keys = await caches.keys();
    await Promise.all(keys.map(function (k) { return k === CACHE ? null : caches.delete(k); }));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url;
  try { url = new URL(req.url); } catch (_) { return; }
  if (url.origin !== self.location.origin) return;
  if (url.pathname.indexOf('/api/') === 0) return;
  if (url.pathname === '/sw.js') return;
  if (url.pathname.indexOf('/camthumb/') === 0) return;   // miniatura ao vivo: sempre rede
  if (url.search) return;                                  // ?query (cache-buster / assinada): passthrough

  // Navegacao SPA -> rede primeiro; offline cai no index
  if (req.mode === 'navigate') {
    e.respondWith((async function () {
      try { return await fetch(req); }
      catch (_) {
        var c = await caches.open(CACHE);
        return (await c.match('/index.html')) || (await c.match('/')) || Response.error();
      }
    })());
    return;
  }

  // Assets imutaveis (hash no nome) -> cache-first SEM revalidar em background
  if (url.pathname.indexOf('/assets/') === 0 ||
      /\.(png|jpg|jpeg|gif|webp|svg|css|js|woff2?|ttf|ico)$/i.test(url.pathname)) {
    e.respondWith((async function () {
      var c = await caches.open(CACHE);
      var hit = await c.match(req);
      if (hit) return hit;
      try { var r = await fetch(req); if (r && r.ok) c.put(req, r.clone()); return r; }
      catch (_) { return Response.error(); }
    })());
    return;
  }
  // resto: passthrough (deixa o navegador tratar)
});
