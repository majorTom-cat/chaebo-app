/* 팀 악보 뷰(뷰 4) — 원본 악보 이미지/PDF 첨부·열람 + 위치 연결(앵커)·실시간 위치 표시.
   (docs/design-team-sheet-2026-07-27 1~3단계. Soundslice 방식 차용: 재생하며 악보를 탭 = "지금 여기".)
   로컬 저장·본인 열람만 — 공유·내보내기 경로 없음(저작권 경계).
   앵커 = [{t: 초, name: 악보 파일명, y: 0~1(그 이미지 안 상대높이)}] — 사이는 시간순 선형 보간. */
(function () {
  'use strict';
  var songId = document.body.dataset.songId;
  var player = Shell.player;
  var loaded = false;
  var anchors = [];        // 시간 오름차순
  var anchorMode = false;  // 켜면: 악보 클릭 = 현재 재생 위치와 연결
  var follow = true;       // 재생 중 하이라이트 따라 화면 스크롤
  var saveTimer = null;

  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }
  function fmt(t) { return Shell.fmt ? Shell.fmt(t) : (Math.floor(t / 60) + ':' + ('0' + Math.floor(t % 60)).slice(-2)); }

  /* ---- 목록 렌더 ---- */
  var pdfLibP = null; // pdf.js(vendor, Apache-2.0) — PDF 를 페이지별 캔버스로 직접 그림.
                      // iframe 내장 뷰어는 클릭을 삼켜 위치 연결이 불가했음(사용자 지적 2026-07-27).
  function pdfLib() {
    if (!pdfLibP) {
      pdfLibP = import('/static/vendor/pdfjs/pdf.min.mjs').then(function (lib) {
        lib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs';
        return lib;
      });
    }
    return pdfLibP;
  }

  function renderPdf(f, holder) {
    return pdfLib().then(function (lib) {
      return lib.getDocument(f.url).promise;
    }).then(function (doc) {
      var chain = Promise.resolve();
      var frag = document.createDocumentFragment();
      for (var n = 1; n <= doc.numPages; n++) {
        (function (pageNo) {
          chain = chain.then(function () { return doc.getPage(pageNo); }).then(function (page) {
            var holderW = holder.clientWidth || 860;
            var vp1 = page.getViewport({ scale: 1 });
            var scale = Math.min((holderW * (window.devicePixelRatio || 1)) / vp1.width, 3);
            var vp = page.getViewport({ scale: scale });
            var item = document.createElement('div');
            item.className = 'teamsheet-item';
            item.dataset.name = f.name + '#p' + pageNo; // 앵커는 페이지 단위로 연결
            var canvas = document.createElement('canvas');
            canvas.width = vp.width; canvas.height = vp.height;
            item.innerHTML = '<span class="teamsheet-name">' + esc(f.name.replace(/^\d{3}_/, ''))
              + (doc.numPages > 1 ? ' · ' + pageNo + '/' + doc.numPages : '') + '</span>'
              + (pageNo === 1 ? '<button type="button" class="btn btn-outline-danger btn-sm teamsheet-del" data-name="' + esc(f.name) + '">삭제</button>' : '');
            item.appendChild(canvas);
            frag.appendChild(item);
            return page.render({ canvasContext: canvas.getContext('2d'), viewport: vp }).promise;
          });
        })(n);
      }
      return chain.then(function () { holder.replaceWith(frag); });
    }).catch(function () {
      // 렌더 실패(깨진 PDF 등) — 내장 뷰어 폴백(보기만 가능, 위치 연결은 안 됨)
      var item = document.createElement('div');
      item.className = 'teamsheet-item';
      item.dataset.name = f.name;
      item.innerHTML = '<span class="teamsheet-name">' + esc(f.name.replace(/^\d{3}_/, '')) + '</span>'
        + '<button type="button" class="btn btn-outline-danger btn-sm teamsheet-del" data-name="' + esc(f.name) + '">삭제</button>'
        + '<iframe src="' + esc(f.url) + '" title="' + esc(f.name) + '"></iframe>';
      holder.replaceWith(item);
    });
  }

  function render(files) {
    var list = document.getElementById('ts-list');
    var empty = document.getElementById('ts-empty');
    empty.hidden = files.length > 0;
    var html = '';
    files.forEach(function (f, i) {
      html += f.pdf
        ? '<div class="teamsheet-pdfholder" data-i="' + i + '"><span class="control-label">PDF 여는 중…</span></div>'
        : '<div class="teamsheet-item" data-name="' + esc(f.name) + '">'
          + '<span class="teamsheet-name">' + esc(f.name.replace(/^\d{3}_/, '')) + '</span>'
          + '<button type="button" class="btn btn-outline-danger btn-sm teamsheet-del" data-name="' + esc(f.name) + '">삭제</button>'
          + '<img src="' + esc(f.url) + '" alt="' + esc(f.name) + '" loading="lazy">'
          + '</div>';
    });
    list.innerHTML = html;
    var pdfJobs = [];
    files.forEach(function (f, i) {
      if (!f.pdf) return;
      var holder = list.querySelector('.teamsheet-pdfholder[data-i="' + i + '"]');
      if (holder) pdfJobs.push(renderPdf(f, holder));
    });
    if (pdfJobs.length) Promise.all(pdfJobs).then(function () { renderAnchors(); updateBand(); window.__pdfRendered = true; });
    renderAnchors();
  }

  function refresh() {
    return fetch('/api/songs/' + songId + '/sheets')
      .then(function (r) { return r.json(); })
      .then(function (d) { render(d.files || []); })
      .catch(function () { /* 오프라인 등 — 조용히(다음 활성화 때 재시도) */ });
  }

  /* ---- 앵커 저장·렌더 ---- */
  function saveAnchors() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      fetch('/api/songs/' + songId + '/sheets/anchors', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ anchors: anchors }),
      }).catch(function () {});
    }, 500);
  }

  function itemOf(name) {
    return document.querySelector('.teamsheet-item[data-name="' + CSS.escape(name) + '"]');
  }

  function renderAnchors() {
    document.querySelectorAll('.ts-anchor').forEach(function (el) { el.remove(); });
    anchors.forEach(function (a, i) {
      var item = itemOf(a.name);
      if (!item) return; // 악보가 지워진 앵커 — 표시만 생략(데이터는 유지)
      var mk = document.createElement('div');
      mk.className = 'ts-anchor';
      mk.style.top = (a.y * 100) + '%';
      mk.dataset.i = i;
      mk.innerHTML = '<span class="ts-anchor-chip">' + fmt(a.t)
        + ' <button type="button" class="ts-anchor-del" aria-label="이 연결 지우기">×</button></span>';
      item.appendChild(mk);
    });
    var n = anchors.length;
    var lab = document.getElementById('ts-anchor-count');
    lab.textContent = n ? '연결 ' + n + '개' : '';
    document.getElementById('ts-band').hidden = n < 2 || !posAt(Shell.visualTime());
  }

  /* ---- 시간 ↔ 악보 위치 보간 ---- */
  function absY(a) { // 앵커의 문서 기준 y(px) — 목록 컨테이너 좌표
    var item = itemOf(a.name);
    if (!item) return null;
    return item.offsetTop + a.y * item.offsetHeight;
  }

  function posAt(t) { // 재생 시각 → 목록 컨테이너 y(px). 앵커 2개 미만이면 null
    if (anchors.length < 2) return null;
    if (t <= anchors[0].t) return absY(anchors[0]);
    for (var i = 0; i < anchors.length - 1; i++) {
      var a = anchors[i], b = anchors[i + 1];
      if (t <= b.t) {
        var ya = absY(a), yb = absY(b);
        if (ya == null || yb == null) return ya != null ? ya : yb;
        var r = (t - a.t) / Math.max(0.001, b.t - a.t);
        return ya + (yb - ya) * r;
      }
    }
    return absY(anchors[anchors.length - 1]);
  }

  function timeAt(item, relY) { // 악보 클릭 위치 → 재생 시각(역보간). 그 지점을 지나는 첫 구간 기준
    if (!anchors.length) return null;
    var y = item.offsetTop + relY * item.offsetHeight;
    for (var i = 0; i < anchors.length - 1; i++) {
      var ya = absY(anchors[i]), yb = absY(anchors[i + 1]);
      if (ya == null || yb == null) continue;
      var lo = Math.min(ya, yb), hi = Math.max(ya, yb);
      if (y >= lo && y <= hi && hi - lo > 1) {
        var r = (y - ya) / (yb - ya);
        return anchors[i].t + (anchors[i + 1].t - anchors[i].t) * Math.max(0, Math.min(1, r));
      }
    }
    // 구간 밖 — 가장 가까운 앵커의 시각
    var best = null, bd = Infinity;
    anchors.forEach(function (a) {
      var ay = absY(a);
      if (ay == null) return;
      var d = Math.abs(ay - y);
      if (d < bd) { bd = d; best = a.t; }
    });
    return best;
  }

  /* ---- 실시간 하이라이트 + 따라가기 ---- */
  function updateBand(t) {
    var band = document.getElementById('ts-band');
    var y = posAt(t == null ? Shell.visualTime() : t);
    if (y == null) { band.hidden = true; return; }
    band.hidden = false;
    band.style.top = y + 'px';
    if (follow && player.isPlaying && player.isPlaying()) {
      var list = document.getElementById('ts-list');
      var abs = list.getBoundingClientRect().top + window.scrollY + y; // 페이지 기준
      var vh = window.innerHeight;
      var cur = window.scrollY;
      if (abs < cur + vh * 0.2 || abs > cur + vh * 0.7) {
        window.scrollTo({ top: Math.max(0, abs - vh * 0.4), behavior: 'smooth' });
      }
    }
  }
  Shell.on('tick', function (t) { updateBand(t); }, 'sheet');
  Shell.on('seek', function (t) { updateBand(t); }, 'sheet');

  /* ---- 업로드·삭제 ---- */
  document.getElementById('ts-file-input').addEventListener('change', function (e) {
    var files = e.target.files;
    if (!files || !files.length) return;
    var fd = new FormData();
    for (var i = 0; i < files.length; i++) fd.append('files', files[i]);
    fetch('/api/songs/' + songId + '/sheets', { method: 'POST', body: fd })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || '올리지 못했어요'); });
        return r.json();
      })
      .then(function (d) { render(d.files || []); })
      .catch(function (err) { alert(err.message || '악보를 올리지 못했어요 — 다시 시도해주세요'); });
    e.target.value = '';
  });

  /* ---- 악보 클릭: 연결 모드=앵커 추가 · 보기 모드=그 위치로 재생 이동 ---- */
  document.getElementById('ts-list').addEventListener('click', function (e) {
    var del = e.target.closest('.teamsheet-del');
    if (del) {
      if (!confirm('이 악보를 지울까요? (앱에서만 지워져요 — 원본 파일은 그대로)')) return;
      fetch('/api/songs/' + songId + '/sheets/' + encodeURIComponent(del.dataset.name), { method: 'DELETE' })
        .then(function (r) { return r.json(); })
        .then(function (d) { render(d.files || []); });
      return;
    }
    var adel = e.target.closest('.ts-anchor-del');
    if (adel) {
      var idx = parseInt(adel.closest('.ts-anchor').dataset.i, 10);
      anchors.splice(idx, 1);
      saveAnchors(); renderAnchors(); updateBand();
      return;
    }
    if (e.target.closest('.ts-anchor')) return; // 칩 나머지 부분 클릭은 무시
    var item = e.target.closest('.teamsheet-item');
    if (!item || !(item.querySelector('img') || item.querySelector('canvas'))) return; // iframe 폴백만 제외
    var rect = item.getBoundingClientRect();
    var relY = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    if (anchorMode) {
      var t = player.currentTime ? player.currentTime() : 0;
      anchors.push({ t: Math.round(t * 1000) / 1000, name: item.dataset.name, y: Math.round(relY * 10000) / 10000 });
      anchors.sort(function (a, b) { return a.t - b.t; });
      saveAnchors(); renderAnchors(); updateBand();
    } else {
      var to = timeAt(item, relY);
      if (to != null) { player.seek(to); updateBand(to); }
    }
  });

  /* ---- 모드·따라가기 토글 ---- */
  document.getElementById('ts-anchor-mode').addEventListener('click', function () {
    anchorMode = !anchorMode;
    this.classList.toggle('active', anchorMode);
    this.setAttribute('aria-pressed', String(anchorMode));
    document.getElementById('ts-list').classList.toggle('ts-anchoring', anchorMode);
    document.getElementById('ts-hint').textContent = anchorMode
      ? '곡을 틀어놓고(또는 원하는 위치에 멈춰두고), 지금 소리가 나는 자리를 악보에서 눌러주세요 — 몇 군데만 연결하면 사이는 자동으로 이어져요'
      : '';
  });
  document.getElementById('ts-follow').addEventListener('change', function (e) {
    follow = e.target.checked;
  });

  Shell.registerView('sheet', {
    init: function () {
      loaded = true;
      Promise.all([
        refresh(),
        fetch('/api/songs/' + songId + '/sheets/anchors').then(function (r) { return r.json(); })
          .then(function (d) { anchors = d.anchors || []; }).catch(function () {}),
      ]).then(function () { renderAnchors(); updateBand(); window.__sheetReady = true; });
    },
    activate: function () { if (loaded) refresh().then(function () { updateBand(); }); },
  });
})();
