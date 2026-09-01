// 站点根作用域的 Service Worker。注册地址是 /sw.js（由 core.views.service_worker 直出本文件），
// 不是 /static/sw.js —— SW 能控制的范围上限就是它脚本 URL 所在的目录，挂在 /static/ 下
// 就只能管静态资源，页面导航完全不被拦，下面的离线降级分支因此一直是死代码。
//
// 扩到根作用域等于让这个文件拦到全站请求，三条硬约束不能破：
//  1. 绝不拦自己。旧版把 /static/sw.js 也归进了静态资源 cache-first 分支，浏览器每次
//     SW 更新检查拿回来的都是缓存里的旧脚本，升 CACHE_VERSION 也不生效 ——
//     「发布后样式滞后一次」的真因就在这。
//  2. 只处理 GET。表单 POST 的 request.mode 同样是 'navigate'，缓存它等于允许重放写操作，
//     而且 cache.put() 遇到非 GET 会直接抛异常。
//  3. 只缓存顶层导航。判据只能用 mode === 'navigate'，不能再用「Accept 含 text/html」：
//     HTMX 的片段请求就是 Accept: text/html,*/*，一旦被缓存，打卡/搜索/聊天会拿到过期片段。
const CACHE_VERSION = 'personal-assistant-v14';

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

// 无网络且该页没缓存时给的降级页
const OFFLINE_HTML = '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">' +
  '<meta name="viewport" content="width=device-width,initial-scale=1">' +
  '<title>离线</title>' +
  '<style>body{font-family:system-ui;display:flex;align-items:center;' +
  'justify-content:center;min-height:100vh;margin:0;background:#ffffff;' +
  'color:#18181b;text-align:center}h1{font-size:1.5rem;margin-bottom:.5rem}' +
  'p{color:#71717a}</style></head>' +
  '<body><div><h1>当前无网络</h1><p>请检查网络连接后重试</p></div></body></html>';

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

function cachePut(event, request, response) {
  if (!response || !response.ok) return;
  // clone() 必须同步做，不能留到 caches.open().then() 里：等那个回调跑起来时原响应已经
  // 被 respondWith 接手开始往页面流了，这时再 clone 直接抛
  // 「TypeError: Response body is already used」，运行时回填全部失败（本地实测就是这个在坏，
  // 表现为离线兜底看起来接得住、但缓存里什么都没存住）
  const clone = response.clone();
  // 并且必须挂到 event.waitUntil 上：即发即忘的 caches.open().then(put) 会在 respondWith
  // 解析后被浏览器直接抽掉（SW 随时可能被回收），同样存不进去
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.put(request, clone))
  );
}

// 网络优先、失败回退缓存：改了静态文件不必记得升 CACHE_VERSION，下次加载就是新的，
// 断网时仍能用缓存兜底
function networkFirst(event, request, fallback) {
  return fetch(request)
    .then((response) => {
      cachePut(event, request, response);
      return response;
    })
    .catch(() => caches.match(request).then((cached) => cached || fallback || Response.error()));
}

// 缓存优先：只给体积稳定、几乎不变的资源（图标、manifest）
function cacheFirst(event, request) {
  return caches.match(request).then((cached) => {
    if (cached) return cached;
    return fetch(request).then((response) => {
      cachePut(event, request, response);
      return response;
    });
  });
}

// 拦截请求
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // 跨域请求交回浏览器默认处理，本 SW 不替第三方站做缓存决定
  if (url.origin !== self.location.origin) return;

  // SW 脚本自身永远直连网络（见文件头第 1 条）
  if (url.pathname === '/sw.js' || url.pathname === '/static/sw.js') return;

  // 非 GET 一律放行（见文件头第 2 条）
  if (request.method !== 'GET') return;

  // CSS / JS：网络优先 + 缓存兜底
  if (url.pathname.startsWith('/static/css/') || url.pathname.startsWith('/static/js/')) {
    event.respondWith(networkFirst(event, request));
    return;
  }

  // 其余静态资源：缓存优先
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(cacheFirst(event, request));
    return;
  }

  // 用户上传的媒体：直连网络，同名覆盖后不该继续看到旧文件
  if (url.pathname.startsWith('/media/')) return;

  // 页面：网络优先 + 离线降级（只有顶层导航进得来，见文件头第 3 条）
  if (request.mode === 'navigate') {
    event.respondWith(
      networkFirst(event, request, new Response(OFFLINE_HTML, {
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
      }))
    );
    return;
  }

  // 其他请求（API、HTMX 片段）：直连网络，一律不缓存
});
