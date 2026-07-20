/* 오프라인 저장 — 곡(타브+스템+파형)을 이 기기(폰)의 IndexedDB 에 받아둔다. 앱+연결(Tailscale·LAN 등)로
   PC 에 닿을 때 '폰에 저장'하면, 그다음엔 PC 를 꺼도 그 곡을 오프라인으로 연습한다(솔로·음소거·배속·A-B).
   개인 기기 로컬 복사(남에게 공유·배포하는 기능이 아니다 — 저작권 경계 유지). SW(sw.js)가 같은 IndexedDB 를
   읽어, 오프라인일 때 /stems·/api 요청을 여기 저장분으로 응답한다 → 연습 화면 코드는 그대로 동작. */
(function () {
  'use strict';
  var DB = 'chaebo-offline', VER = 1;

  function openDB() {
    return new Promise(function (res, rej) {
      var r = indexedDB.open(DB, VER);
      r.onupgradeneeded = function (e) {
        var db = e.target.result;
        if (!db.objectStoreNames.contains('files')) db.createObjectStore('files');  // key = pathname
        if (!db.objectStoreNames.contains('songs')) db.createObjectStore('songs');  // key = String(songId)
      };
      r.onsuccess = function () { res(r.result); };
      r.onerror = function () { rej(r.error); };
    });
  }
  function _op(store, mode, fn) {
    return openDB().then(function (db) {
      return new Promise(function (res, rej) {
        var t = db.transaction(store, mode), os = t.objectStore(store), out;
        var q = fn(os);
        if (q) q.onsuccess = function () { out = q.result; };
        t.oncomplete = function () { res(out); };
        t.onerror = function () { rej(t.error); };
        t.onabort = function () { rej(t.error); };
      });
    });
  }
  function put(store, key, val) { return _op(store, 'readwrite', function (os) { return os.put(val, key); }); }
  function get(store, key) { return _op(store, 'readonly', function (os) { return os.get(key); }); }
  function del(store, key) { return _op(store, 'readwrite', function (os) { return os.delete(key); }); }
  function all(store) { return _op(store, 'readonly', function (os) { return os.getAll(); }); }
  function keys(store) { return _op(store, 'readonly', function (os) { return os.getAllKeys(); }); }

  function saveUrl(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error(url + ' (' + r.status + ')');
      return r.blob().then(function (b) {
        var path = new URL(url, location.href).pathname;
        return put('files', path, { blob: b, type: r.headers.get('content-type') || 'application/octet-stream', at: Date.now() });
      });
    });
  }

  // 현재 연습 화면이 쓰는 스크립트·CSS 를 캐시에 담는다(오프라인에 앱 코드가 있어야 화면이 뜬다).
  //   ★첫 방문 땐 SW 가 아직 제어 전이라 이 자산들이 런타임 캐시에 안 담긴다(고전 PWA 문제) → 저장 시 명시적으로.
  //   전용 캐시('chaebo-offline-assets')에 담아도 SW 의 caches.match 는 전 캐시를 뒤지므로 오프라인에 서빙된다.
  function cacheAssets() {
    if (!('caches' in window)) return Promise.resolve();
    var urls = {};
    document.querySelectorAll('script[src], link[rel=stylesheet]').forEach(function (el) {
      var u = el.src || el.href;
      if (u && u.indexOf(location.origin) === 0) urls[u] = 1;
    });
    // ★동적 import(스크립트 태그 아님)라 위 스캔이 놓치는 벤더 — 재생엔진(스템 배속·피치)이 여기 의존. 명시 추가.
    ['/static/vendor/signalsmith-stretch/SignalsmithStretch.mjs',
     '/static/fonts/Pacifico.woff2'].forEach(function (p) { urls[location.origin + p] = 1; });
    return caches.open('chaebo-offline-assets').then(function (c) {
      return Promise.all(Object.keys(urls).map(function (u) { return c.add(u).catch(function () {}); }));
    }).catch(function () {});
  }

  // 곡 하나를 폰에 저장 — 메타·타브·파형·연습 화면 HTML·모든 스템(오디오) + 앱 코드(스크립트·CSS).
  //   onProgress(done, total, label).
  function saveSong(songId, onProgress) {
    onProgress = onProgress || function () {};
    var meta;
    return cacheAssets().then(function () {
      return fetch('/api/songs/' + songId, { cache: 'no-store' });
    }).then(function (r) { return r.json(); }).then(function (m) {
      meta = m;
      var urls = ['/api/songs/' + songId, '/api/songs/' + songId + '/tab',
        '/api/songs/' + songId + '/peaks', '/api/songs/' + songId + '/state',
        '/api/settings', '/songs/' + songId + '/practice'];
      var stems = m.stems || {};
      Object.keys(stems).forEach(function (k) { if (stems[k]) urls.push(stems[k]); });
      var total = urls.length, done = 0;
      return urls.reduce(function (chain, u) {
        return chain.then(function () { return saveUrl(u); })
          .catch(function () { /* 한 파일 실패해도 계속(부분 저장) */ })
          .then(function () { done++; onProgress(done, total, u); });
      }, Promise.resolve()).then(function () {
        return put('songs', String(songId), {
          id: songId, title: meta.title || ('곡 ' + songId),
          stems: Object.keys(stems), at: Date.now(),
        });
      });
    });
  }

  function removeSong(songId) {
    return get('songs', String(songId)).then(function (s) {
      if (!s) return;
      // 이 곡에 딸린 파일들(경로가 /…/{songId}/… 또는 /songs/{songId}/…) 삭제
      return keys('files').then(function (ks) {
        var mine = ks.filter(function (k) {
          return k.indexOf('/' + songId + '/') >= 0 || k.indexOf('/songs/' + songId + '/') >= 0
            || k.indexOf('/api/songs/' + songId) === 0 || k.indexOf('/stems/' + songId + '/') === 0;
        });
        return Promise.all(mine.map(function (k) { return del('files', k); }));
      }).then(function () { return del('songs', String(songId)); });
    });
  }

  // 폰에 저장된 곡을 '지금 접속 중인 PC'로 보낸다(업로드→그 PC 가 재분석 없이 곡으로 추가). 개인 기기 간
  //   이동(폰이 나른다) — GPU PC 에서 분석→폰→다른 PC. 반환: {ok, song_id, title}.
  function sendToPc(songId, onProgress) {
    onProgress = onProgress || function () {};
    var id = String(songId);
    return Promise.all([
      get('files', '/api/songs/' + id),
      get('files', '/api/songs/' + id + '/tab'),
      get('files', '/api/songs/' + id + '/peaks'),
    ]).then(function (r) {
      var metaF = r[0], tabF = r[1], peaksF = r[2];
      if (!metaF || !tabF) throw new Error('저장 데이터가 부족해요');
      return Promise.all([metaF.blob.text(), tabF.blob.text()]).then(function (t) {
        var meta = JSON.parse(t[0]);
        var fd = new FormData();
        fd.append('meta', t[0]);
        fd.append('tab', t[1]);
        if (peaksF && peaksF.blob) fd.append('peaks', peaksF.blob, 'peaks.json');
        var stems = meta.stems || {}, names = Object.keys(stems), i = 0;
        return names.reduce(function (chain, name) {
          var path = new URL(stems[name], location.href).pathname;
          return chain.then(function () { return get('files', path); }).then(function (f) {
            if (f && f.blob) fd.append('stem_' + name, f.blob, name + '.m4a');
            onProgress(++i, names.length);
          });
        }, Promise.resolve()).then(function () {
          return fetch('/api/import', { method: 'POST', body: fd }).then(function (resp) {
            if (!resp.ok) throw new Error('보내기 실패 (' + resp.status + ')');
            return resp.json();
          });
        });
      });
    });
  }

  function savedSongs() { return all('songs'); }
  function isSaved(songId) { return get('songs', String(songId)).then(function (s) { return !!s; }); }
  function usageBytes() {
    return all('files').then(function (fs) { return fs.reduce(function (n, f) { return n + (f.blob ? f.blob.size : 0); }, 0); });
  }

  window.chaeboOffline = {
    saveSong: saveSong, removeSong: removeSong, savedSongs: savedSongs,
    isSaved: isSaved, usageBytes: usageBytes, sendToPc: sendToPc,
  };
})();
