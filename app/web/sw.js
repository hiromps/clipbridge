/* ClipBridge Service Worker — iPhone 用画面の「殻」だけをキャッシュする。
 *
 * 目的: ハブにしている PC の電源が落ちていても、ホーム画面のアイコンから画面を開いて
 * 別の PC に切り替えられるようにする。API（/api/*）や PC 用画面（/pc）は一切触らない。
 * サーバーが __VERSION__ を実際の版に置き換えて配るので、版が変わるとキャッシュも入れ替わる。
 */
const VERSION = '__VERSION__';
const CACHE = 'clipbridge-shell-' + VERSION;
const SHELL = ['/', '/style.css', '/app.js', '/img/flow-overview.webp', '/img/add-to-home.webp', '/img/shortcut-setup.webp', '/img/done.webp', '/icon-180.png', '/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  const isIndex = url.pathname === '/';
  if (!isIndex && !SHELL.includes(url.pathname)) return; // /pc, /api, manifest などは素通し

  const cacheKey = isIndex ? '/' : url.pathname;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      // ネットワーク優先: 更新をすぐ反映し、成功したらキャッシュを更新する
      const response = await fetch(request);
      const landedOnIndex = !response.redirected && new URL(response.url).pathname === '/';
      if (response.ok && response.type === 'basic' && (!isIndex || landedOnIndex)) {
        cache.put(cacheKey, response.clone());
      }
      return response;
    } catch (error) {
      // オフライン（ハブ PC が落ちている）: キャッシュした殻を返す。app.js が別の PC へ接続できる
      const cached = await cache.match(cacheKey);
      if (cached) return cached;
      throw error;
    }
  })());
});
