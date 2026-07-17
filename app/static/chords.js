/* 코드 악보 뷰 — 격자 렌더·코드 클릭 수정·현재 마디 하이라이트만.
   플레이어·트랜스포트·상태·저장은 셸(shell.js)이 문서당 1개 소유. */
(function () {
  'use strict';

  var songId = Shell.songId;
  var player = Shell.player;
  var byBar = {};
  var barsCount = 0;
  var curBar = -1;
  var meta = null; // 분석 메타(slots·bar_slots·offset·chords…) — 셸 브로드캐스트 수신
  var _gridDirty = false; // 비활성 중 메타 변경 → 활성화 때 renderGrid 1회(코드검사 2026-07-17)

  /* ---- 현재 마디 하이라이트 — 재생 진행을 코드 격자 위에 ---- */
  function timeToBar(t) {
    if (!meta || !meta.bpm) return -1;
    var bs = meta.bar_slots || 16;
    var slots = meta.slots;
    if (slots && slots.length > 1) {
      if (t < slots[0]) return -1;
      var lo = 0, hi = slots.length - 1;
      while (lo < hi) { var mid = (lo + hi + 1) >> 1; if (slots[mid] <= t) lo = mid; else hi = mid - 1; }
      return Math.floor(lo / bs);
    }
    var slotDur = 60 / meta.bpm / (bs === 48 && meta.meter !== '12/8' ? 12 : 4);
    return Math.floor((t - (meta.offset || 0)) / slotDur / bs);
  }
  function highlightBar(t) {
    var b = timeToBar(t);
    if (b === curBar) return;
    var prev = document.querySelector('.cbar.now');
    if (prev) prev.classList.remove('now');
    var cell = document.querySelector('.cbar[data-bar="' + b + '"]');
    if (cell) cell.classList.add('now');
    curBar = b;
  }

  /* ---- 셸 훅 — 코드 뷰 활성일 때만 ---- */
  Shell.on('tick', function () { highlightBar(Shell.visualTime()); }, 'chords');
  Shell.on('seek', function () { highlightBar(Shell.visualTime()); }, 'chords');
  Shell.on('sync', function () { highlightBar(Shell.visualTime()); }, 'chords');

  /* ---- 가사(받아쓰기) — 소절 시각 → 마디 매핑 ---- */
  var lyricByBar = {};   // bar -> {text, indexes:[segIdx], manual}
  var lyricPoll = null;

  function buildLyricMap(t) {
    lyricByBar = {};
    var ly = t.lyrics;
    var btn = document.getElementById('btn-lyrics');
    var st = ly && ly.status;
    if (btn) {
      btn.hidden = st === 'ready';
      btn.disabled = st === 'running' || st === 'queued';
      btn.textContent = (st === 'running' || st === 'queued') ? '가사 가져오는 중…'
        : (st === 'error' ? '가사 다시 가져오기' : '가사 가져오기');
      btn.title = st === 'error' ? (ly.error || '지난 번 가사 가져오기가 실패했어요')
        : '인터넷 가사(LRCLIB)를 먼저 찾고, 없으면 보컬 소리에서 받아써요. 정확한 가사는 「전체 가사」에서 붙여넣을 수도 있어요';
    }
    // 진행 중이면 완료까지 셸 메타 폴링(가사는 분석과 별개 잡)
    if (st === 'running' || st === 'queued') {
      if (!lyricPoll) lyricPoll = setInterval(function () { Shell.refreshMeta(); }, 3000);
    } else if (lyricPoll) {
      clearInterval(lyricPoll); lyricPoll = null;
    }
    if (!ly || st !== 'ready' || !ly.segments) return;
    // 걸친 마디들에 단어 분배 — 시작 마디에만 몰리지 않게(사용자 실증 2026-07-10)
    lyricByBar = Shell.spreadLyrics(ly.segments, function (tt) {
      return Math.max(0, timeToBar(tt));
    });
  }

  /* ---- 분석 메타(셸 브로드캐스트) → 격자 데이터 ---- */
  Shell.on('meta', function (t) {
    meta = t;
    buildLyricMap(t);
    if (t.status === 'ready' && t.notes && t.notes.length) {
      var bs = t.bar_slots || 16;
      var lastGi = 0;
      t.notes.forEach(function (n) { lastGi = Math.max(lastGi, n.gi + n.glen); });
      barsCount = Math.ceil(lastGi / bs);
      byBar = groupByBar(t.chords);
      var kd = '';
      if (t.key_json && t.key_json.label) {
        kd = t.key_json.display || t.key_json.label;
        var mn = t.key_json.minor || (t.key_json.mode === 'minor' ? t.key_json.label : '');
        if (mn) kd += ' (' + mn + ')'; // 메이저 곡도 상대 단조 병기(사용자 지시)
      }
      var bpmDisp = t.meter === '12/8' ? Math.round(t.bpm / 3) : Math.round(t.bpm);
      document.getElementById('sheet-meta').textContent =
        (kd ? '키 ' + kd + ' · ' : '') + 'BPM ' + bpmDisp + ' · ' + (t.meter || '4/4');
      document.getElementById('chord-grid').hidden = false;
      document.getElementById('sheet-empty').hidden = true;
      if (Shell.active() === 'chords') {  // 비활성이면 재렌더 스킵(코드검사: refreshMeta·폴링마다 격자 전량 재생성 낭비)
        renderGrid();
        highlightBar(Shell.visualTime());
      } else {
        _gridDirty = true;  // 코드뷰 활성화 때 1회 렌더
      }
      metaOnce();
    } else {
      document.getElementById('chord-grid').hidden = true;
      document.getElementById('sheet-empty').hidden = false;
      metaOnce();
    }
  });
  var metaOnce = function () {}; // 첫 메타 도착 신호 — 준비 플래그용(아래에서 배선)

  /* ---- 코드 격자 렌더 + 클릭 수정 ---- */
  var editingBar = null;

  function groupByBar(chords) { // 마디 -> [{pos,label,manual}] (pos 오름차순) — 반마디 코드 지원
    var m = {};
    (chords || []).forEach(function (c) { (m[c.bar] = m[c.bar] || []).push(c); });
    Object.keys(m).forEach(function (b) {
      m[b].sort(function (a, c2) { return (a.pos || 0) - (c2.pos || 0); });
    });
    return m;
  }
  // 코드 라벨을 '베이스가 치는 음' 강조로 포맷(사용자 요청 2026-07-17: 베이스 음 크게·색). 슬래시면 슬래시
  // 음(F/C 의 C), 없으면 근음(Fm 의 F)이 베이스 → 그 부분을 .cbass 로 감싸 크게·진하게. 성질(m 등)은 그대로.
  function fmtChord(label) {
    var parts = String(label).split('/');
    if (parts.length > 1) {
      return _esc(parts[0]) + '<span class="cbass">/' + _esc(parts[1]) + '</span>';
    }
    var m = parts[0].match(/^([A-G][#b]?)(.*)$/);
    return m ? '<span class="cbass">' + _esc(m[1]) + '</span>' + _esc(m[2]) : _esc(parts[0]);
  }
  function _esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;'); }
  // 구간 색(같은 그룹=같은 색 → 반복되는 벌스/코러스가 한눈에). 은은한 배경.
  var SEC_COLORS = ['rgba(240,168,72,.16)', 'rgba(120,150,205,.15)', 'rgba(205,120,140,.15)',
    'rgba(120,190,150,.15)', 'rgba(190,160,110,.16)', 'rgba(165,140,205,.15)'];
  function sectionByBar() {
    var sd = meta && meta.sections;
    var arr = (sd && sd.sections) || (Array.isArray(sd) ? sd : []);
    var map = {};
    arr.forEach(function (s) {
      var b = timeToBar((s.s || 0) + 0.05);
      if (b >= 0 && map[b] === undefined) map[b] = { name: s.name || '구간', grp: s.group || 0, vocal: s.has_vocal !== false };
    });
    return map;
  }
  function renderGrid() {
    // 마디 안 여러 코드는 셀을 '위치 비례 칸'으로 실분할(사용자 지시 2026-07-10 — 'Am / Dm' 글자
    // 나열은 어느 박에서 바뀌는지 안 보임). 칸 폭 = 그 코드가 차지하는 슬롯 수
    var grid = document.getElementById('chord-grid');
    var bs = (meta && meta.bar_slots) || 16;
    var html = '';
    var prev = null; // 직전 '칸'의 라벨(마디 경계 넘어 이어짐) — 반복은 옅게
    var secMap = sectionByBar(); // 구간(인트로/벌스/코러스) 시작 마디 → 라벨(사용자 요청 2026-07-17)
    for (var b = 0; b < barsCount; b++) {
      if (secMap[b]) {  // 구간 시작 마디 앞에 전폭 헤더 — 같은 그룹(반복 구간)은 같은 색
        var sc = secMap[b];
        html += '<div class="cbar-section" style="background:' + SEC_COLORS[sc.grp % SEC_COLORS.length] + '">' +
          _esc(sc.name) + (sc.vocal ? '' : ' · 연주') + '</div>';
      }
      var entries = byBar[b] || [];
      var segs = '';
      if (!entries.length) {
        var cls0 = 'chord empty';
        segs = '<span class="cseg"><span class="' + cls0 + '">' + (prev ? '%' : '·') + '</span></span>';
      } else {
        var sizeCls = entries.length >= 3 ? ' multi' : (entries.length === 2 ? ' duo' : '');
        for (var i = 0; i < entries.length; i++) {
          var c = entries[i];
          var next = i + 1 < entries.length ? (entries[i + 1].pos || 0) : bs;
          var w = Math.max(1, next - (c.pos || 0));
          var cls = 'chord' + sizeCls;
          if (c.label === prev) cls += ' same';
          if (c.manual) cls += ' manual';
          prev = c.label;
          segs += '<span class="cseg" style="flex:' + w + '">' +
            '<span class="' + cls + '">' + fmtChord(c.label) + '</span></span>';
        }
      }
      var lyr = lyricByBar[b];
      var lyrHtml = lyr
        ? '<span class="cbar-lyric' + (lyr.manual ? ' manual' : '') + '">' + lyr.text + '</span>'
        : '';
      html += '<div class="cbar" data-bar="' + b + '" title="누르면 코드·가사를 고칠 수 있어요 (여러 코드는 띄어서: Fm Db)">' +
        '<span class="cbar-num">' + (b + 1) + '</span>' +
        '<span class="cbar-chords">' + segs + '</span>' + lyrHtml + '</div>';
    }
    grid.innerHTML = html;
    curBar = -1;
    grid.querySelectorAll('.cbar').forEach(function (cell) {
      cell.addEventListener('click', function () { openEditor(cell); });
    });
  }

  var editingLyric = null;   // 열 때의 소절 원문 — 바뀐 경우에만 저장
  var editingSegIdxs = null; // 편집 대상 소절 인덱스(수정=첫 소절, 나머지는 병합 삭제)

  function lyricEditTarget(bar) {
    // 표시는 마디에 분배되지만 수정은 '소절(문장)' 단위 — 이 마디에서 시작한 소절 우선,
    // 없으면(앞 마디에서 걸쳐 온 경우) 걸친 소절 하나를 통째로 수정
    var lyr = lyricByBar[bar];
    if (!lyr) return null;
    return lyr.startIdxs.length ? lyr.startIdxs : lyr.spanIdxs.slice(0, 1);
  }

  function openEditor(cell) {
    var editor = document.getElementById('chord-editor');
    editingBar = parseInt(cell.dataset.bar, 10);
    var entries = byBar[editingBar] || [];
    document.getElementById('chord-input').value =
      entries.map(function (c) { return c.label; }).join(' ');
    var li = document.getElementById('lyric-input');
    var segs = (meta && meta.lyrics && meta.lyrics.segments) || [];
    editingSegIdxs = lyricEditTarget(editingBar);
    editingLyric = editingSegIdxs
      ? editingSegIdxs.map(function (i) { return segs[i].text; }).join(' ')
      : null;
    li.value = editingLyric || '';
    li.disabled = !editingSegIdxs;
    li.placeholder = editingSegIdxs ? '' : '이 마디엔 받아쓴 소절이 없어요';
    li.title = editingSegIdxs ? '소절(문장) 단위로 고쳐요 — 여러 마디에 걸친 소절이면 전체 문장' : '';
    document.getElementById('chord-editor-title').textContent = (editingBar + 1) + '마디 코드';
    editor.hidden = false;
    var rect = cell.getBoundingClientRect();
    var wrap = document.getElementById('sheet-wrap').getBoundingClientRect();
    editor.style.left = Math.max(0, Math.min(rect.left - wrap.left, wrap.width - 240)) + 'px';
    editor.style.top = (rect.bottom - wrap.top + 6) + 'px';
    document.getElementById('chord-input').focus();
  }
  function closeEditor() {
    document.getElementById('chord-editor').hidden = true;
    editingBar = null;
  }

  function saveLyricIfChanged() {
    // 소절 단위 저장 — 첫 대상 소절에 전체 문안, 같은 마디에서 시작한 나머지 소절은 병합 삭제
    if (editingBar == null || !editingSegIdxs) return Promise.resolve();
    var li = document.getElementById('lyric-input');
    if (li.disabled || li.value.trim() === (editingLyric || '')) return Promise.resolve();
    var text = li.value.trim();
    var chain = fetch('/api/songs/' + songId + '/lyrics', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index: editingSegIdxs[0], text: text }),
    });
    editingSegIdxs.slice(1).sort(function (a, b) { return b - a; }).forEach(function (idx) {
      chain = chain.then(function () {
        return fetch('/api/songs/' + songId + '/lyrics', {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ index: idx, text: '' }),
        });
      });
    });
    return chain;
  }

  function submitChord(label) {
    if (editingBar == null) return;
    saveLyricIfChanged().then(function () {
      return fetch('/api/songs/' + songId + '/chords', {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bar: editingBar, label: label }),
      });
    }).then(function (r) {
      if (!r.ok) { alert('저장하지 못했어요 — 잠시 후 다시 시도해주세요'); return null; }
      return r.json();
    }).then(function (res) {
      if (!res) return;
      byBar = groupByBar(res.chords);
      closeEditor();
      Shell.refreshMeta(); // 가사·믹서 스트립·타브 심볼까지 같은 문서 — 신선도 전파(격자도 재렌더)
    });
  }

  /* ---- 뷰 모듈 등록 ---- */
  Shell.registerView('chords', {
    init: function () {
      document.getElementById('chord-cancel').addEventListener('click', closeEditor);
      document.getElementById('chord-save').addEventListener('click', function () {
        submitChord(document.getElementById('chord-input').value.trim());
      });
      document.getElementById('chord-auto').addEventListener('click', function () { submitChord(''); });
      document.getElementById('chord-input').addEventListener('keydown', function (e) {
        if (e.key === 'Enter') submitChord(this.value.trim());
        if (e.key === 'Escape') closeEditor();
      });
      document.getElementById('btn-print-chords').addEventListener('click', function () {
        window.open('/songs/' + songId + '/chords/print', '_blank');
      });
      document.getElementById('btn-lyrics').addEventListener('click', function () {
        fetch('/api/songs/' + songId + '/lyrics', { method: 'POST' })
          .then(function () { Shell.refreshMeta(); }); // 진행 폴링은 buildLyricMap 이 시작
      });

      // 준비 플래그 — 스템(재생)과 격자(첫 메타)가 둘 다 준비된 뒤
      var metaArrived = meta ? Promise.resolve()
        : new Promise(function (res) { metaOnce = res; });
      Promise.all([Shell.ready, metaArrived]).then(function () {
        highlightBar(Shell.visualTime());
        window.__chordsReady = true;
      });
    },
    activate: function () {
      if (_gridDirty && meta) { renderGrid(); _gridDirty = false; }  // 비활성 중 밀린 메타 반영
      highlightBar(Shell.visualTime());
    },
  });
})();
