/* AI 狼人杀 —— service worker
 *
 * 它只干两件事：
 *   1. 让"添加到主屏幕"之后能像 APP 一样启动（配合 manifest.webmanifest）
 *   2. 图标这类不变的东西走缓存，打开更快
 *
 * ⚠️ 页面本身（index.html）刻意用 **network-first**：这个项目我一直在改，
 * 缓存优先会让手机永远跑旧版本 —— 那种"改了没用"的坑比省几百毫秒难受得多。
 */
const CACHE = 'wolf-v1';
const SHELL = ['./manifest.webmanifest', './icon-192.png', './icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;                       // 行动/语音上传一律直连
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // 接口一律不缓存：状态、台账、语音都必须是最新的
  if (url.pathname.startsWith('/api/')) return;

  if (req.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('index.html')) {
    e.respondWith(
      fetch(req)
        .then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); return res; })
        .catch(() => caches.match(req))                   // 断网时用上次的页面兜底
    );
    return;
  }
  if (/\.(png|jpg|svg|ico|webmanifest)$/.test(url.pathname)) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); return res;
    })));
  }
});
