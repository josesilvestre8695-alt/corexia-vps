// build 2026-08-18-conciliacao
// SW pass-through: limpa o cache velho UMA vez e depois passa tudo direto pela rede.
// Sem cache (nada fica velho) e sem unregister (sem loop de recarga).
self.addEventListener('install', function(){ self.skipWaiting(); });
self.addEventListener('activate', function(e){
  e.waitUntil((async function(){
    try {
      var keys = await caches.keys();
      await Promise.all(keys.map(function(k){ return caches.delete(k); }));
    } catch(x) {}
    try { await self.clients.claim(); } catch(x) {}
  })());
});
self.addEventListener('fetch', function(){ /* passa direto pela rede, sem cachear */ });
