/* Corexia — Service Worker */
const CACHE = 'corexia-v1';

// URLs que NUNCA devem ser cacheadas (dados dinâmicos / streams / vídeos)
const NETWORK_ONLY_PATTERNS = ['/api/', '/img/', '/gravacao/', '/webhookAlertas'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(['/', '/manifest.json']))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;

  // (a) Não-GET ou rotas dinâmicas: network only, nunca cachear
  if (
    request.method !== 'GET' ||
    NETWORK_ONLY_PATTERNS.some((p) => request.url.includes(p))
  ) {
    return; // deixa o browser tratar direto na rede
  }

  // (b) Navegação: network-first com fallback ao cache de '/'
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put('/', copy)).catch(() => {});
          return response;
        })
        .catch(() => caches.match('/'))
    );
    return;
  }

  // (c) Assets com hash no nome: cache-first
  if (request.url.includes('/assets/')) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request).then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
          }
          return response;
        });
      })
    );
    return;
  }

  // (d) Resto: network com fallback ao cache
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
        }
        return response;
      })
      .catch(() => caches.match(request))
  );
});

/* ---------- NOTIFICAÇÃO PUSH (alerta da IA) ---------- */
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data && event.data.text() }; }
  const title = data.title || 'Corexia — Alerta';
  const options = {
    body: data.body || 'Novo alerta de segurança.',
    icon: '/brand/logo-icon.png',      // logo Corexia
    badge: '/brand/logo-icon.png',
    tag: data.tag || 'corexia-alerta', // agrupa por câmera
    renotify: true,
    requireInteraction: true,          // fica na tela até o usuário interagir
    vibrate: [200, 100, 200, 100, 200],
    data: { url: data.url || '/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ('focus' in w) { w.navigate ? w.navigate(target) : null; return w.focus(); }
      }
      return self.clients.openWindow(target);
    })
  );
});
