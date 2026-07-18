/* chaebo 서비스워커 — 휴대폰에 '앱처럼' 설치·오프라인 동작.
   버전드 캐시(chaebo-shell-<버전>): 새 버전 배포 시 이 파일의 VERSION 문자열이 바뀌어 브라우저가
   변경을 감지 → 새 셸을 받아 캐시 → 옛 캐시 정리 → pwa.js 가 '새 버전 받기' 안내. (데스크톱 update.js 와
   별개 — 그건 PC 서버 파일 교체, 이건 폰 캐시 갱신.) 곡 데이터/스템 오프라인 저장은 IndexedDB(별도, 2단계). */
const VERSION = '__CHAEBO_VERSION__';
const SHELL_CACHE = 'chaebo-shell-' + VERSION;

/* 오프라인에도 켜지게 미리 받아두는 최소 셸(네비게이션 진입점). 나머지 정적 자산(css/js/폰트/alphaTab)은
   처음 온라인 방문 때 아래 fetch 핸들러가 자동 캐시(런타임)한다 — 목록을 빠짐없이 나열 안 해도 됨. */
const PRECACHE = ['/tuner', '/', '/manifest.webmanifest',
  '/static/tokens.css', '/static/ui.css', '/static/logo.js', '/static/pwa.js', '/static/tuner.js',
  '/static/icons/icon-192.png', '/static/icons/icon-512.png'];

self.addEventListener('install', (e) => {
  // ★skipWaiting 은 여기서 하지 않는다 — 새 버전은 '대기' 상태로 두고 pwa.js 가 '새 버전 받기'를 띄운다.
  //   사용자가 '받기'를 눌러야(postMessage 'skipWaiting') 활성화 → 새로고침. (install 에서 즉시 skip 하면
  //   프롬프트 없이 자동 리로드돼 버림.) 첫 설치는 대기할 SW 가 없어 그대로 활성화된다.
  e.waitUntil(
    caches.open(SHELL_CACHE)
      .then((c) => Promise.allSettled(PRECACHE.map((u) => c.add(u))))  // 하나 실패해도 설치 진행
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k.startsWith('chaebo-shell-') && k !== SHELL_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;    // 외부 요청은 건드리지 않음(외부 전송 0)

  // 곡 데이터·스템은 2단계(IndexedDB)에서 오프라인 처리 — 지금은 네트워크로만.
  if (url.pathname.startsWith('/stems/') || url.pathname.startsWith('/api/')) return;

  // 정적 자산 + manifest → 캐시 우선(없으면 받아서 캐시). 폰트·alphaTab 등도 처음 방문에 자동 저장.
  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest') {
    e.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((resp) => {
        if (resp && resp.ok) {
          const clone = resp.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(req, clone));
        }
        return resp;
      }).catch(() => hit))
    );
    return;
  }

  // 페이지(네비게이션) → 네트워크 우선, 끊기면 캐시 → 그래도 없으면 튜너(오프라인 최소 동작).
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).then((resp) => {
        if (resp && resp.ok) {
          const clone = resp.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(req, clone));
        }
        return resp;
      }).catch(() => caches.match(req).then((hit) => hit || caches.match('/tuner')))
    );
    return;
  }
});

self.addEventListener('message', (e) => {
  if (e.data === 'skipWaiting') self.skipWaiting();
});
