/* Corexia SW kill-switch — PWA removido do portal. Desregistra o SW e limpa o cache. */
self.addEventListener('install', function(e){ self.skipWaiting(); });
self.addEventListener('activate', function(e){ e.waitUntil((async function(){
  try{ var ks = await caches.keys(); await Promise.all(ks.map(function(k){ return caches.delete(k); })); }catch(_){}
  try{ await self.registration.unregister(); }catch(_){}
})()); });
self.addEventListener('fetch', function(e){ /* passthrough: nao intercepta nada */ });
