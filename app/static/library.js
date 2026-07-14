/* 라이브러리 화면 — 시안 01(카드 상태)·02(추가 모달) 충실 구현.
   카드 마크업·문구는 시안 토씨 그대로. 상태: ready/separating/downloading/queued/error/empty */
(function () {
  'use strict';

  var grid = document.getElementById('song-grid');
  var search = document.getElementById('search');
  var songs = [];

  var ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
  var ICON_WARN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 18H2L12 3z"/><line x1="12" y1="9" x2="12" y2="14"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
  var ICON_RETRY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0114.13-3.36L23 10M1 14l5.36 4.36A9 9 0 0020.49 15"/></svg>';
  var ICON_COPY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';

  function copyText(text, btn) {
    var done = function () {
      var old = btn.innerHTML;
      btn.innerHTML = ICON_COPY + ' 복사됨';
      setTimeout(function () { btn.innerHTML = old; }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
    } else {
      fallbackCopy(text, done);
    }
  }
  function fallbackCopy(text, done) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { /* 복사 실패는 조용히 무시 */ }
    document.body.removeChild(ta);
  }

  function esc(s) {
    // 따옴표까지 이스케이프 — 속성값에 넣기(data-title="..." 등). div.innerHTML 은 " ' 를 안 바꿔서
    // 유튜브 제목의 " 가 속성을 깨고 이벤트핸들러 주입 여지까지 있었음(코드리뷰 2026-07-14).
    return (s == null ? '' : String(s)).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmtDur(sec) {
    if (!sec && sec !== 0) return '—:—';
    var m = Math.floor(sec / 60), s = Math.round(sec % 60);
    return m + ':' + String(s).padStart(2, '0');
  }

  function dots(ready) {
    return '<div class="stem-dots ' + (ready ? 'stem-dots-ready' : 'stem-dots-pending') + '">' +
      '<span class="dot"></span>'.repeat(6) + '</div>';
  }

  function card(song) {
    var del = '<button type="button" class="song-card-delete" data-del="' + song.id +
      '" data-title="' + esc(song.title) + '" aria-label="곡 삭제" title="곡 삭제">×</button>';
    // 제목·설명 수정(사용자 요청 2026-07-10) — 유튜브 제목/파일명 그대로는 불편
    var edit = '<button type="button" class="song-card-edit" data-edit="' + song.id +
      '" aria-label="제목·설명 고치기" title="제목·설명 고치기">✎</button>';
    var desc = song.description
      ? '<p class="song-desc">' + esc(song.description) + '</p>' : '';
    var top = del + edit + '<div class="song-card-top"><h3 class="song-title">' + esc(song.title) +
      '</h3><span class="song-duration">' + fmtDur(song.duration) + '</span></div>' + desc;

    if (song.status === 'ready') {
      return '<a class="song-card" href="/songs/' + song.id + '/practice">' + top + dots(true) +
        '<span class="song-status status-ok">' + ICON_CHECK + ' 재생 준비 완료</span></a>';
    }
    if (song.status === 'error') {
      var errText = song.error || '처리에 실패했어요';
      return '<div class="song-card song-card-danger" data-state="failed">' + top +
        '<p class="song-error">' + ICON_WARN + ' <span class="song-error-text">' + esc(errText) + '</span></p>' +
        '<div class="song-error-actions">' +
        '<button type="button" class="btn btn-outline-danger btn-sm" data-retry="' + song.id + '">' +
        ICON_RETRY + ' 다시 시도</button>' +
        '<button type="button" class="btn btn-ghost btn-sm" data-copyerr="' + esc(errText) + '">' +
        ICON_COPY + ' 사유 복사</button></div></div>';
    }
    if (song.status === 'stopped') {
      return '<div class="song-card" data-state="stopped">' + top + dots(false) +
        '<p class="song-stage">멈췄어요 — 분석을 중지했어요</p>' +
        '<div class="song-error-actions">' +
        '<button type="button" class="btn btn-outline btn-sm" data-retry="' + song.id + '">' +
        ICON_RETRY + ' 다시 분석</button></div></div>';
    }
    var pct = Math.round(song.progress || 0);
    var stage = song.status === 'downloading' ? '유튜브에서 받는 중'
      : song.status === 'separating' ? '분리 중 — 6개 악기로 나누는 중'
      : '대기 중';
    var badge = song.status === 'separating' && window.__device === 'cpu'
      ? '<span class="badge badge-muted">CPU 모드(느림)</span>' : '';
    return '<div class="song-card" data-state="' + esc(song.status) + '">' + top + dots(false) +
      '<div class="progress-row"><div class="progress-bar"><div class="progress-fill" style="width: ' + pct + '%"></div></div>' +
      '<span class="progress-pct">' + pct + '%</span></div>' +
      '<p class="song-stage">' + stage + '</p>' + badge +
      '<div class="song-error-actions"><button type="button" class="btn btn-ghost btn-sm" data-cancel="' + song.id + '">중지</button></div></div>';
  }

  function render() {
    var q = (search.value || '').trim().toLowerCase();
    var shown = songs.filter(function (s) { return !q || (s.title || '').toLowerCase().indexOf(q) !== -1; });
    document.body.classList.toggle('is-empty', songs.length === 0);
    grid.innerHTML = shown.map(card).join('');
    grid.querySelectorAll('[data-retry]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        fetch('/api/songs/' + btn.dataset.retry + '/retry', { method: 'POST' }).then(refresh);
      });
    });
    grid.querySelectorAll('[data-copyerr]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        copyText(btn.dataset.copyerr, btn);
      });
    });
    grid.querySelectorAll('[data-edit]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation(); // ready 카드는 <a> — 연습 화면 이동 방지
        var s = songs.filter(function (x) { return String(x.id) === btn.dataset.edit; })[0];
        if (!s) return;
        editingId = s.id;
        document.getElementById('edit-title').value = s.title || '';
        document.getElementById('edit-artist').value = s.artist || '';
        document.getElementById('edit-desc').value = s.description || '';
        document.getElementById('edit-modal').hidden = false;
        document.getElementById('edit-title').focus();
      });
    });
    grid.querySelectorAll('[data-del]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation(); // ready 카드는 <a> — 연습 화면 이동 방지
        if (!confirm('「' + btn.dataset.title + '」을(를) 삭제할까요?\n원본과 분리 결과가 함께 지워져요.')) return;
        fetch('/api/songs/' + btn.dataset.del, { method: 'DELETE' }).then(refresh);
      });
    });
    grid.querySelectorAll('[data-cancel]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        fetch('/api/songs/' + btn.dataset.cancel + '/cancel', { method: 'POST' }).then(refresh);
      });
    });
  }

  function refresh() {
    return fetch('/api/songs').then(function (r) { return r.json(); }).then(function (data) {
      songs = data;
      render();
    });
  }

  search.addEventListener('input', render);

  fetch('/api/system').then(function (r) { return r.json(); }).then(function (d) {
    window.__device = d.device;
    document.getElementById('device-label').textContent = d.device === 'gpu' ? 'GPU 모드' : 'CPU 모드';
    render(); // 장치 정보가 첫 렌더보다 늦게 와도 CPU 배지 반영
  });

  /* ---- 제목·설명 수정 모달 (사용자 요청 2026-07-10) ---- */
  var editingId = null;
  document.getElementById('btn-close-edit').addEventListener('click', function () {
    document.getElementById('edit-modal').hidden = true;
  });
  document.getElementById('btn-save-edit').addEventListener('click', function () {
    var t = document.getElementById('edit-title').value.trim();
    if (!t) { alert('제목을 비울 수는 없어요'); return; }
    fetch('/api/songs/' + editingId, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: t,
        artist: document.getElementById('edit-artist').value,
        description: document.getElementById('edit-desc').value,
      }),
    }).then(function (r) {
      if (!r.ok) { alert('저장하지 못했어요 — 잠시 후 다시 시도해주세요'); return; }
      document.getElementById('edit-modal').hidden = true;
      refresh();
    });
  });

  refresh();
  // 진행률 갱신 (REQ-OPS-001) — 작업 중일 때만 촘촘히, 유휴엔 8초(상시 2초 폴링은 낭비)
  (function pollLoop() {
    var active = songs.some(function (s) {
      return s.status === 'queued' || s.status === 'downloading' || s.status === 'separating';
    });
    setTimeout(function () { refresh().then(pollLoop, pollLoop); }, active ? 2000 : 8000);
  })();

  /* ---- 곡 추가 모달 ---- */
  var modal = document.getElementById('add-modal');
  var main = document.getElementById('page-main');

  function openModal() {
    modal.hidden = false;
    main.classList.add('backdrop');
    main.setAttribute('aria-hidden', 'true');
  }
  function closeModal() {
    modal.hidden = true;
    main.classList.remove('backdrop');
    main.removeAttribute('aria-hidden');
    hideErr('yt'); hideErr('file');
  }
  document.getElementById('btn-open-add').addEventListener('click', openModal);
  document.getElementById('btn-open-add-empty').addEventListener('click', openModal);
  document.getElementById('btn-close-add').addEventListener('click', closeModal);
  modal.addEventListener('click', function (e) { if (e.target === modal) closeModal(); });

  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.tab-btn').forEach(function (b) {
        b.classList.remove('active');
        b.setAttribute('aria-selected', 'false');
      });
      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
      document.querySelectorAll('.tab-panel').forEach(function (p) {
        p.hidden = p.dataset.panel !== btn.dataset.tab;
      });
    });
  });

  function showErr(kind, msg) {
    document.getElementById(kind + '-error-text').textContent = msg;
    document.getElementById(kind + '-error').hidden = false;
  }
  function hideErr(kind) {
    document.getElementById(kind + '-error').hidden = true;
  }

  document.getElementById('btn-add-url').addEventListener('click', function () {
    hideErr('yt');
    var url = document.getElementById('yt-url').value.trim();
    if (!/^https?:\/\/(www\.)?(youtube\.com|youtu\.be)\//.test(url)) {
      showErr('yt', '지원하지 않는 주소예요 — 유튜브 링크인지 확인해주세요');
      return;
    }
    fetch('/api/songs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: url }),
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || '추가에 실패했어요'); });
      document.getElementById('yt-url').value = '';
      closeModal();
      refresh();
    }).catch(function (e) { showErr('yt', e.message); });
  });

  document.getElementById('btn-add-file').addEventListener('click', function () {
    hideErr('file');
    var input = document.getElementById('file-input');
    if (!input.files || !input.files.length) {
      showErr('file', '먼저 파일을 선택해주세요');
      return;
    }
    var fd = new FormData();
    fd.append('file', input.files[0]);
    fetch('/api/songs/upload', { method: 'POST', body: fd }).then(function (r) {
      if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || '추가에 실패했어요'); });
      input.value = '';
      closeModal();
      refresh();
    }).catch(function (e) { showErr('file', e.message); });
  });

  var dz = document.getElementById('drop-zone');
  dz.addEventListener('dragover', function (e) { e.preventDefault(); });
  dz.addEventListener('drop', function (e) {
    e.preventDefault();
    if (e.dataTransfer.files.length) document.getElementById('file-input').files = e.dataTransfer.files;
  });

  /* ---- 첫 실행 GPU 안내 + 설치 중 배너 (사용자 요청 2026-07-13) — GPU 있는데 CPU torch면
     분석 전에 켜라고 안내(모르고 CPU로 돌리다 중간 전환 안 되던 문제), 설치 중엔 왜 대기하는지 알림 ---- */
  (function gpuGuide() {
    fetch('/api/system').then(function (r) { return r.json(); }).then(function (s) {
      if (!s || !s.can_enable_gpu) return;
      if (localStorage.getItem('gpu_onboard_dismissed') === '1') return;
      var ov = document.createElement('div');
      ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:1300;';
      ov.innerHTML = '<div style="background:#fff;color:#222;max-width:420px;width:92vw;padding:22px 24px;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.35);">' +
        '<h3 style="margin:0 0 8px">그래픽카드(GPU)가 있어요</h3>' +
        '<p style="color:#555;font-size:14px;line-height:1.6;margin:0 0 16px">이 PC엔 NVIDIA 그래픽카드가 있어요. GPU 가속을 켜면 곡 분석이 <b>훨씬 빨라져요</b>. 한 번만 설정하면 되고(약 2.5GB 내려받기), <b>분석을 시작하기 전에</b> 켜 두는 게 좋아요.</p>' +
        '<div style="display:flex;justify-content:flex-end;gap:8px;">' +
        '<button type="button" id="gpu-guide-later" class="btn btn-ghost btn-sm">나중에</button>' +
        '<button type="button" id="gpu-guide-now" class="btn btn-primary btn-sm">GPU 켜러 가기</button></div></div>';
      document.body.appendChild(ov);
      function dismiss() { localStorage.setItem('gpu_onboard_dismissed', '1'); ov.remove(); }
      ov.querySelector('#gpu-guide-later').addEventListener('click', dismiss);
      ov.querySelector('#gpu-guide-now').addEventListener('click', function () {
        localStorage.setItem('gpu_onboard_dismissed', '1'); location.href = '/settings';
      });
      ov.addEventListener('click', function (e) { if (e.target === ov) ov.remove(); });
    }).catch(function () { /* 시스템 조회 실패는 안내 생략 */ });

    var gpuBanner = null;
    (function pollGpu() {
      fetch('/api/gpu/progress').then(function (r) { return r.json(); }).then(function (p) {
        if (p && p.running) {
          if (!gpuBanner) {
            gpuBanner = document.createElement('div');
            gpuBanner.style.cssText = 'margin:10px 12px;padding:8px 12px;background:#fff7e0;border:1px solid #e8d48a;border-radius:8px;font-size:13px;color:#6b5900;';
            var g = document.getElementById('song-grid');
            if (g && g.parentNode) g.parentNode.insertBefore(gpuBanner, g);
          }
          gpuBanner.textContent = 'GPU 켜는 중이에요 — 설치가 끝나면 기다리던 분석이 이어져요.' + (p.message ? ' (' + p.message + ')' : '');
        } else if (gpuBanner) { gpuBanner.remove(); gpuBanner = null; }
      }).catch(function () { /* no-op */ }).then(function () { setTimeout(pollGpu, 4000); });
    })();
  })();

  /* ---- 업데이트 확인(사용자 요청 2026-07-13) — 공개 version.json 대비 새 버전이면 1회 안내(업데이트/무시) ---- */
  (function updateCheck() {
    fetch('/api/update-check').then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.newer || !d.latest) return;
      if (localStorage.getItem('update_dismissed') === d.latest) return; // 이 버전은 '나중에' 누름
      var ov = document.createElement('div');
      ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:1400;';
      ov.innerHTML = '<div class="upd-box" style="background:#fff;color:#222;max-width:440px;width:92vw;padding:22px 24px;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.35);">' +
        '<h3 style="margin:0 0 8px">새 버전 v' + esc(d.latest) + ' 이 나왔어요</h3>' +
        '<p style="color:#555;font-size:14px;line-height:1.6;margin:0 0 16px">지금은 v' + esc(d.current) + ' 이에요.' +
        (d.notes ? '<br>' + esc(d.notes) : '') +
        '<br><b>빠른 업데이트</b>는 바뀐 부분만 몇 초 만에 받아요(엔진·설정·곡은 그대로).</p>' +
        '<div style="display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px;">' +
        '<button type="button" id="upd-later" class="btn btn-ghost btn-sm">나중에</button>' +
        (d.url ? '<button type="button" id="upd-get" class="btn btn-outline btn-sm">직접 받기</button>' : '') +
        '<button type="button" id="upd-quick" class="btn btn-primary btn-sm">빠른 업데이트</button>' +
        '</div></div>';
      document.body.appendChild(ov);
      var box = ov.querySelector('.upd-box');
      ov.querySelector('#upd-later').addEventListener('click', function () {
        localStorage.setItem('update_dismissed', d.latest); ov.remove();
      });
      var getBtn = ov.querySelector('#upd-get');
      if (getBtn) getBtn.addEventListener('click', function () { window.open(d.url, '_blank'); });
      ov.querySelector('#upd-quick').addEventListener('click', function () {
        var qb = ov.querySelector('#upd-quick');
        qb.disabled = true; qb.textContent = '업데이트 받는 중…';
        // 델타 적용 → 자동으로 앱을 껐다 켠다(사용자 요청 2026-07-13). 공용 로직(update.js).
        window.chaeboUpdate.applyAndRestart(function (stage, data) {
          if (stage === 'applied') {
            box.innerHTML = '<h3 style="margin:0 0 8px">다 됐어요!</h3>' +
              '<p style="color:#555;font-size:14px;line-height:1.6;margin:0">새 버전(v' +
              esc((data && data.version) || d.latest) + ')을 적용하려고 <b>앱을 자동으로 껐다 켜요.</b>' +
              '<br>잠시 뒤 새 창(또는 새 탭)으로 다시 열려요.</p>';
          } else if (stage === 'restarting') {
            box.innerHTML = '<h3 style="margin:0 0 8px">다시 시작하는 중…</h3>' +
              '<p style="color:#555;font-size:14px;line-height:1.6;margin:0">앱을 껐다 켜고 있어요. 잠시만 기다려 주세요.</p>';
          } else if (stage === 'already-current') {
            // 이미 최신 — 재시작 없이 안내만(무한 업데이트 루프 방지)
            box.innerHTML = '<h3 style="margin:0 0 8px">이미 최신이에요</h3>' +
              '<p style="color:#555;font-size:14px;line-height:1.6;margin:0 0 16px">지금 버전이 최신이라 다시 받을 필요가 없어요.</p>' +
              '<div style="display:flex;justify-content:flex-end"><button type="button" id="upd-ok2" class="btn btn-primary btn-sm">알겠어요</button></div>';
            localStorage.setItem('update_dismissed', d.latest);  // 이 버전은 다시 안 물음
            box.querySelector('#upd-ok2').addEventListener('click', function () { ov.remove(); });
          } else if (stage === 'apply-failed') {
            qb.disabled = false; qb.textContent = '빠른 업데이트';
            alert('빠른 업데이트에 실패했어요 — "직접 받기"로 설치해 주세요.');
          }
        });
      });
      ov.addEventListener('click', function (e) { if (e.target === ov) ov.remove(); });
    }).catch(function () { /* 조회 실패는 조용히(로컬 우선) */ });
  })();
})();
