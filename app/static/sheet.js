/* 팀 악보 뷰(뷰 4) — 원본 악보 이미지/PDF 첨부·열람 (docs/design-team-sheet-2026-07-27 1단계).
   로컬 저장·본인 열람만 — 공유·내보내기 경로 없음(저작권 경계). 앵커 싱크·실시간 표시는 2·3단계. */
(function () {
  'use strict';
  var songId = document.body.dataset.songId;
  var loaded = false;

  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }

  function render(files) {
    var list = document.getElementById('ts-list');
    var empty = document.getElementById('ts-empty');
    empty.hidden = files.length > 0;
    var html = '';
    files.forEach(function (f) {
      html += '<div class="teamsheet-item">'
        + '<span class="teamsheet-name">' + esc(f.name.replace(/^\d{3}_/, '')) + '</span>'
        + '<button type="button" class="btn btn-outline-danger btn-sm teamsheet-del" data-name="' + esc(f.name) + '">삭제</button>'
        + (f.pdf
          ? '<iframe src="' + esc(f.url) + '" title="' + esc(f.name) + '"></iframe>'
          : '<img src="' + esc(f.url) + '" alt="' + esc(f.name) + '" loading="lazy">')
        + '</div>';
    });
    list.innerHTML = html;
  }

  function refresh() {
    return fetch('/api/songs/' + songId + '/sheets')
      .then(function (r) { return r.json(); })
      .then(function (d) { render(d.files || []); })
      .catch(function () { /* 오프라인 등 — 조용히(다음 활성화 때 재시도) */ });
  }

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

  document.getElementById('ts-list').addEventListener('click', function (e) {
    var btn = e.target.closest('.teamsheet-del');
    if (!btn) return;
    if (!confirm('이 악보를 지울까요? (앱에서만 지워져요 — 원본 파일은 그대로)')) return;
    fetch('/api/songs/' + songId + '/sheets/' + encodeURIComponent(btn.dataset.name), { method: 'DELETE' })
      .then(function (r) { return r.json(); })
      .then(function (d) { render(d.files || []); });
  });

  Shell.registerView('sheet', {
    init: function () { loaded = true; refresh(); window.__sheetReady = true; },
    activate: function () { if (loaded) refresh(); }, // 다른 기기/폰에서 올렸을 수 있어 재조회(싼 GET)
  });
})();
