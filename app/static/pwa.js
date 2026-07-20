/* PWA 등록 + '새 버전 받기' 안내 — 모든 페이지가 로드(설치·오프라인·업데이트 담당).
   서비스워커(/sw.js)는 보안 컨텍스트(https 또는 localhost)에서만 등록된다 — LAN 평문에선 조용히 건너뜀
   (그땐 설치가 안 될 뿐, 앱은 정상). 새 버전은 SW 가 감지 → 아래 토스트로 '받기' → skipWaiting+새로고침.

   ★데스크톱 앱 창(127.0.0.1/localhost)에선 SW 를 쓰지 않는다 — PWA(설치·오프라인)는 '폰·다른 기기에서 원격
   접속'용이고, 데스크톱 앱은 자체 업데이트(빠른 업데이트)가 따로 있다. 앱 창에 SW 를 등록하면 캐시·네비게이션
   폴백(/tuner)·controllerchange 재로드가 얽혀 창이 안 뜨는 재로드 정황이 관측됐다(2026-07-20, 실증). 그래서
   로컬 호스트에선 등록을 건너뛰고, 예전에 등록돼 있던 SW·셸 캐시는 정리한다. 원격(폰: 100.x·192.168.x·*.ts.net
   등 비-로컬 호스트)에서만 SW 를 켠다. */
(function () {
  'use strict';
  if (!('serviceWorker' in navigator) || !window.isSecureContext) return;

  var h = location.hostname;
  var isLocalApp = h === '127.0.0.1' || h === 'localhost' || h === '::1' || h === '' || !!window.pywebview;
  if (isLocalApp) {
    // 앱 창/로컬 브라우저: 혹시 등록돼 있던 SW 해제 + 셸 캐시 삭제(오래된 재로드 루프 상태를 스스로 푼다).
    try {
      navigator.serviceWorker.getRegistrations().then(function (rs) {
        rs.forEach(function (r) { r.unregister(); });
      }).catch(function () {});
      if (window.caches) caches.keys().then(function (ks) {
        ks.forEach(function (k) { if (k.indexOf('chaebo-shell') === 0) caches.delete(k); });
      }).catch(function () {});
    } catch (e) { /* 무시 */ }
    return;
  }

  var reloaded = false;
  var hadController = !!navigator.serviceWorker.controller;   // 로드 시점에 이미 제어 중인 SW 가 있었나
  navigator.serviceWorker.addEventListener('controllerchange', function () {
    // 새 SW 가 제어권을 잡으면 1회 새로고침(새 버전 반영). 단, '첫 설치'(무 컨트롤러 상태에서 claim)
    // 로 인한 controllerchange 는 새로고침하지 않는다 — 업데이트(구 SW 교체)일 때만.
    if (reloaded || !hadController) return;
    reloaded = true;
    window.location.reload();
  });

  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js').then(function (reg) {
      // 이미 대기 중인 새 버전이 있으면 바로 안내
      if (reg.waiting && navigator.serviceWorker.controller) showUpdate(reg.waiting);
      reg.addEventListener('updatefound', function () {
        var nw = reg.installing;
        if (!nw) return;
        nw.addEventListener('statechange', function () {
          // 새 SW 가 설치됐고 이미 구버전이 돌고 있으면 = 업데이트 대기
          if (nw.state === 'installed' && navigator.serviceWorker.controller) showUpdate(nw);
        });
      });
      // 앱을 열 때마다 새 버전 확인(온라인일 때만 실제 갱신됨)
      try { reg.update(); } catch (e) { /* 무시 */ }
    }).catch(function () { /* 등록 실패해도 앱은 정상 */ });
  });

  function showUpdate(worker) {
    if (document.getElementById('pwa-update-bar')) return;
    var bar = document.createElement('div');
    bar.id = 'pwa-update-bar';
    bar.setAttribute('role', 'status');
    bar.style.cssText = 'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:9999;' +
      'display:flex;align-items:center;gap:12px;background:#20242e;color:#e8eaf0;border:1px solid #f0a848;' +
      'border-radius:12px;padding:10px 14px;box-shadow:0 6px 24px rgba(0,0,0,.35);font-size:14px;max-width:92vw';
    var txt = document.createElement('span');
    txt.textContent = '새 버전이 있어요';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '받기';
    btn.style.cssText = 'border:none;background:#f0a848;color:#1a1206;font-weight:700;border-radius:8px;' +
      'padding:6px 14px;cursor:pointer;font-size:14px';
    btn.addEventListener('click', function () {
      btn.textContent = '받는 중…';
      btn.disabled = true;
      worker.postMessage('skipWaiting');   // 새 SW 활성화 → controllerchange → 새로고침
    });
    var later = document.createElement('button');
    later.type = 'button';
    later.textContent = '나중에';
    later.style.cssText = 'border:none;background:transparent;color:#8a91a0;cursor:pointer;font-size:13px';
    later.addEventListener('click', function () { bar.remove(); });
    bar.appendChild(txt); bar.appendChild(btn); bar.appendChild(later);
    document.body.appendChild(bar);
  }
})();
