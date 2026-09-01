const CACHE_VERSION = 'personal-assistant-v10';

// 预缓存的核心静态资源（已自托管，不再依赖 CDN）
const PRECACHE_URLS = [
  '/static/css/custom.css',
  '/static/js/tailwind.js',
  '/static/js/htmx.min.js',
  '/static/js/chart.umd.min.js',
  '/static/js/pinyin-pro.js',
  '/static/js/offline-queue.js',
  '/static/js/quick-parse.js',
  '/static/manifest.json',
];

// 安装：预缓存核心资源
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

// 激活：清理旧版本缓存
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_VERSION)
          .map((key) => caches.delete(key))
      )
    ).then(() => self.clients.claim())
  );
});

// 拦截请求
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // 静态资源：cache-first
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request).then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(request, clone));
          }
          return response;
        });
      })
    );
    return;
  }

  // HTML 页面：network-first + 离线降级
  if (request.mode === 'navigate' || request.headers.get('accept')?.includes('text/html')) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(request, clone));
          }
          return response;
        })
        .catch(() =>
          caches.match(request).then((cached) => {
            if (cached) return cached;
            // 无缓存：返回简单离线页面
            return new Response(
              '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">' +
              '<meta name="viewport" content="width=device-width,initial-scale=1">' +
              '<title>离线</title>' +
              '<style>body{font-family:system-ui;display:flex;align-items:center;' +
              'justify-content:center;min-height:100vh;margin:0;background:#ffffff;' +
              'color:#18181b;text-align:center}h1{font-size:1.5rem;margin-bottom:.5rem}' +
              'p{color:#71717a}</style></head>' +
              '<body><div><h1>当前无网络</h1><p>请检查网络连接后重试</p></div></body></html>',
              { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
            );
          })
        )
    );
    return;
  }

  // 其他请求：直接走网络
  event.respondWith(fetch(request));
});
