/* 资源雷达 Service Worker — App Shell 缓存 */
var CACHE_NAME = 'res-radar-v1';
var APP_SHELL = [
  '/',
  '/static/css/all.min.css',
  '/site-settings.js',
  '/static/js/perf.js',
  '/static/manifest.json'
];

self.addEventListener('install', function (e) {
  e.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(APP_SHELL).catch(function () {});
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.filter(function (k) { return k !== CACHE_NAME; })
            .map(function (k) { return caches.delete(k); })
      );
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (e) {
  var url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  /* 只缓存同源静态资源，API 走网络优先 */
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(e.request).catch(function () {
        return new Response('{"error":"offline"}', {
          headers: { 'Content-Type': 'application/json' }
        });
      })
    );
    return;
  }
  if (url.pathname.startsWith('/static/') || url.pathname === '/' || url.pathname === '/site-settings.js') {
    e.respondWith(
      caches.match(e.request).then(function (cached) {
        var fetched = fetch(e.request).then(function (resp) {
          if (resp && resp.status === 200) {
            var clone = resp.clone();
            caches.open(CACHE_NAME).then(function (c) { c.put(e.request, clone); });
          }
          return resp;
        }).catch(function () { return cached; });
        return cached || fetched;
      })
    );
  }
});
