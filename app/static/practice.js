/* 믹서 뷰 — 시안 03 충실 구현 + 파형 줌(REQ-PLAY-013, rev8).
   플레이어·트랜스포트·상태·저장은 셸(shell.js) 소유 — 여기는 파형·줌·스템 채널·루프 칩·코드 스트립만.
   파형: 서버 8000버킷 피크 envelope → 줌 창(window)을 룰러·전 lane·오버레이가 공유,
   재생 중엔 플레이헤드 추종(DAW 표준). */
(function () {
  'use strict';

  var player = Shell.player;
  var state = Shell.state;      // 살아있는 공유 객체 — 재할당 금지
  var transport = Shell.transport;
  var fmt = Shell.fmt;
  function saveState() { Shell.save(); }

  // 시안 순서: 베이스(주력 첫줄)·드럼·보컬·기타·건반·그 외 + 품질 고지(기타·건반·그 외 — SEP-003 rev 2026-07-07)
  var STEMS = [
    { key: 'bass', label: '베이스', color: 'var(--stem-bass)', note: false },
    { key: 'drums', label: '드럼', color: 'var(--stem-drums)', note: false },
    { key: 'vocals', label: '보컬', color: 'var(--stem-vocals)', note: false },
    { key: 'guitar', label: '기타', color: 'var(--stem-guitar)', note: true },
    { key: 'piano', label: '건반', color: 'var(--stem-piano)', note: true },
    { key: 'other', label: '그 외', color: 'var(--stem-other)', note: true },
  ];
  var MAX_ZOOM = 256; // ×8→×64→×256 (사용자 지시 2026-07-08 ×2) — peaks v4(256000버킷, 0.9ms)가 뒷받침
  var DRAW_POINTS = 1200; // 창을 화면 픽셀 수준으로 축약해 그림
  var LANE_H = 96, LANE_MID = 48, LANE_AMP = 94; // 파형 lane 확대(48→76→96px — 사용자 피드백 ×2)

  var grid = document.getElementById('deck-grid');
  var peaksData = {};
  var zoom = 1;
  var viewStart = 0; // 창 시작(초)
  // 스템별 파형 세로 확대(진폭) 배율 — 파형은 가장 큰 악기 기준 정규화라 조용한 베이스가 작게 보인다.
  // 레인마다 '세로 ×' 버튼으로 1→2→4 배 키워 본다(사용자 요청 2026-07-14). 재생·논리엔 무영향(표시만).
  var vamp = {};

  function windowDur() { return (player.duration() || 1) / zoom; }

  function clampView() {
    var dur = player.duration() || 1;
    viewStart = Math.max(0, Math.min(viewStart, dur - windowDur()));
  }

  function visualTime() { return Shell.visualTime(); }

  /* ---- 스템 행 렌더 ---- */
  function stemRow(s) {
    var controls = document.createElement('div');
    controls.className = 'stem-controls';
    controls.style.setProperty('--stem-color', s.color);
    controls.innerHTML =
      '<div class="stem-controls-row">' +
      '<span class="stem-chip"></span>' +
      '<span class="stem-name">' + s.label + '</span>' +
      '<button type="button" class="stem-toggle" data-solo="' + s.key + '" aria-pressed="false" aria-label="' + s.label + ' 솔로" title="이 악기만 듣기">솔로</button>' +
      '<button type="button" class="stem-toggle" data-mute="' + s.key + '" aria-pressed="false" aria-label="' + s.label + ' 음소거" title="이 악기 끄기">음소거</button>' +
      '<button type="button" class="stem-toggle" data-vamp="' + s.key + '" aria-label="' + s.label + ' 파형 세로 확대" title="이 악기 파형을 세로로 크게 보기(진폭)">세로 1×</button>' +
      '<input type="range" class="stem-volume" min="0" max="100" value="100" data-volume="' + s.key + '" aria-label="' + s.label + ' 볼륨">' +
      '</div>';

    var lane = document.createElement('div');
    lane.className = 'stem-lane-cell';
    lane.style.setProperty('--stem-color', s.color);
    lane.dataset.lane = s.key;
    lane.innerHTML = '<svg class="mini-wave" viewBox="0 0 ' + DRAW_POINTS + ' ' + LANE_H + '" preserveAspectRatio="none" role="img" aria-label="' + s.label + ' 파형"></svg>';

    grid.appendChild(controls);
    grid.appendChild(lane);

    if (s.note) {
      var p = document.createElement('p');
      p.className = 'stem-quality-note-row';
      p.textContent = '⚠ 분리 품질이 낮을 수 있어요';
      grid.appendChild(p);
    }
  }

  /* ---- 파형·룰러: 현재 창 [viewStart, viewStart+windowDur] 렌더 ---- */
  function drawWaves() {
    var dur = player.duration() || 1;
    var w = windowDur();
    STEMS.forEach(function (s) {
      var all = peaksData[s.key] || [];
      var n = all.length;
      if (!n) return;
      var i0 = Math.floor(viewStart / dur * n);
      var i1 = Math.min(n, Math.ceil((viewStart + w) / dur * n));
      var slice = all.slice(i0, i1);
      // 창 버킷 → 화면 포인트 축약(구간 max — 피크 보존)
      var pts = Math.min(DRAW_POINTS, slice.length);
      var top = [], bottom = [];
      for (var i = 0; i < pts; i++) {
        var a = Math.floor(i / pts * slice.length);
        var b = Math.max(a + 1, Math.floor((i + 1) / pts * slice.length));
        var v = 0;
        for (var j = a; j < b; j++) if (slice[j] > v) v = slice[j];
        // 세로 확대 배율 적용 후 lane 높이 안으로 클램프(넘치면 잘려도 위아래 대칭 유지).
        var h = Math.min(LANE_MID - 0.5, Math.max(0.6, v * LANE_AMP * (vamp[s.key] || 1)) / 2);
        var x = (i / pts * DRAW_POINTS).toFixed(1);
        top.push(x + ',' + (LANE_MID - h).toFixed(2));
        bottom.push(x + ',' + (LANE_MID + h).toFixed(2));
      }
      bottom.reverse();
      var svg = document.querySelector('[data-lane="' + s.key + '"] .mini-wave');
      svg.innerHTML = '<polygon class="bar" points="' + top.join(' ') + ' ' + bottom.join(' ') + '"/>';
    });
    drawRuler();
    renderSections();
    renderChordStrip();
    renderOverlay();
  }

  /* ---- 코드 스트립 (REQ-CHORD-002) — 타브 분석의 코드 진행, 줌 창과 같은 x-스케일.
     마디 단위가 아니라 '변화 지점' 목록 — 반마디 코드(pos)도 제 위치에(사용자 지시 2026-07-10) ---- */
  var chordData = null; // {points: [{t, label}] 시각 오름차순}

  function renderChordStrip() {
    if (!chordData || !chordData.points.length) return;
    var strip = document.getElementById('chord-strip');
    var w = windowDur();
    var end = viewStart + w;
    var pts = chordData.points;
    var stripPx = strip.clientWidth || 700;
    var html = '';
    var cursor = viewStart;
    for (var i = 0; i < pts.length; i++) {
      var s = pts[i].t;
      var e = i + 1 < pts.length ? pts[i + 1].t : Math.max(player.duration() || 0, s);
      var left = Math.max(s, viewStart);
      var right = Math.min(e, end);
      if (right <= left) continue;
      if (left > cursor) { // 첫 코드 전(인트로 스크랩) 여백 — 자리만 채움
        html += '<span class="chord-seg" style="width:' + ((left - cursor) / w * 100).toFixed(2) + '%"></span>';
      }
      var pctW = (right - left) / w * 100;
      // 너무 좁은 구간은 라벨 생략(뭉개진 글자 방지) — 줌 인 하면 보인다
      var label = (pctW / 100 * stripPx) >= 20 ? pts[i].label : '';
      html += '<span class="chord-seg" data-i="' + i + '" style="width:' + pctW.toFixed(2) + '%">' + label + '</span>';
      cursor = right;
    }
    strip.innerHTML = html;
    updateCurrentChord(true);
  }

  var lastChordIdx = null;

  function updateCurrentChord(force) {
    if (!chordData) return;
    var t = visualTime();
    var pts = chordData.points;
    var idx = -1;
    for (var i = 0; i < pts.length; i++) {
      if (pts[i].t <= t) idx = i; else break;
    }
    if (!force && idx === lastChordIdx) return;
    lastChordIdx = idx;
    document.querySelectorAll('.chord-seg[data-i]').forEach(function (el) {
      el.classList.toggle('chord-seg-current', parseInt(el.dataset.i, 10) === idx);
    });
  }

  /* ---- 곡 구간 띠 — 자동 경계·같은 색=반복 유형. 클릭=그 구간 A-B, 더블클릭=이름(사람 몫) ---- */
  var sectionsData = null; // [{s,e,group,name,has_vocal,manual?}]
  var SECT_COLORS = ['rgba(240,168,72,0.30)', 'rgba(93,155,224,0.30)', 'rgba(121,196,124,0.30)',
    'rgba(196,121,196,0.30)', 'rgba(224,150,93,0.30)', 'rgba(150,150,170,0.30)'];

  function renderSections() {
    if (!sectionsData || !sectionsData.length) return;
    var strip = document.getElementById('sect-strip');
    var w = windowDur();
    var end = viewStart + w;
    var stripPx = strip.clientWidth || 700;
    var html = '';
    var cursor = viewStart;
    sectionsData.forEach(function (sec, i) {
      var left = Math.max(sec.s, viewStart);
      var right = Math.min(sec.e, end);
      if (right <= left) return;
      if (left > cursor) {
        html += '<span class="sect-seg" style="flex-basis:' + ((left - cursor) / w * 100).toFixed(2) + '%;background:transparent;border:none"></span>';
      }
      var pctW = (right - left) / w * 100;
      var wide = (pctW / 100 * stripPx) >= 34;
      var label = wide ? sec.name + (sec.has_vocal ? '' : ' <span class="no-vocal">(노래 없음)</span>') : '';
      html += '<span class="sect-seg' + (sec.manual ? ' manual' : '') + '" data-i="' + i +
        '" style="flex-basis:' + pctW.toFixed(2) + '%;background:' + SECT_COLORS[sec.group % SECT_COLORS.length] + '"' +
        ' title="' + sec.name + ' (' + fmt(sec.s) + '–' + fmt(sec.e) + ') · 클릭=이 구간 반복 · 더블클릭=이름 바꾸기">' +
        label + '</span>';
      cursor = right;
    });
    strip.innerHTML = html;
  }

  /* ---- 현재 가사 줄(받아쓰기 초안) — 재생 위치 소절 + 다음 소절 미리보기 ---- */
  var lyricSegs = [];
  var lastLyricIdx = null;

  function updateNowLyric(force) {
    if (!lyricSegs.length) return;
    var t = visualTime();
    var idx = -1;
    for (var i = 0; i < lyricSegs.length; i++) {
      if (lyricSegs[i].s <= t) idx = i; else break;
    }
    if (!force && idx === lastLyricIdx) return;
    lastLyricIdx = idx;
    var cur = idx >= 0 && t <= lyricSegs[idx].e + 1.5 ? lyricSegs[idx] : null;
    var next = lyricSegs[idx + 1] || null;
    document.getElementById('now-lyric-cur').textContent = cur ? cur.text : (next ? '♪' : '');
    document.getElementById('now-lyric-next').textContent = next ? '다음: ' + next.text : '';
  }

  // 분석 메타(셸 브로드캐스트) → 코드 변화 지점 — 배지·메트로놈 게이트는 셸 몫
  Shell.on('meta', function (t) {
    var ly = t.lyrics;
    if (ly && ly.status === 'ready' && ly.segments && ly.segments.length) {
      lyricSegs = ly.segments;
      document.getElementById('now-lyric').hidden = false;
      updateNowLyric(true);
    }
    var sc = t.sections;
    if (sc && sc.status === 'ready' && sc.sections && sc.sections.length) {
      sectionsData = sc.sections;
      document.getElementById('sect-label').hidden = false;
      document.getElementById('sect-strip').hidden = false;
      renderSections();
    }
    if (t.status !== 'ready' || !t.bpm || !t.chords || !t.chords.length) return;
    var BS = t.bar_slots || 16;
    var barDur = (BS / 4) * (60 / t.bpm); // 균일 폴백 — 동적 그리드(slots)가 있으면 그것을
    function slotTime(g) {
      if (t.slots && t.slots.length) {
        if (g < t.slots.length) return t.slots[g];
        return t.slots[t.slots.length - 1] + (g - t.slots.length + 1) * barDur / BS;
      }
      return (t.offset || 0) + g * barDur / BS;
    }
    var points = [];
    t.chords.slice().sort(function (a, b) {
      return (a.bar - b.bar) || ((a.pos || 0) - (b.pos || 0));
    }).forEach(function (c) {
      if (points.length && points[points.length - 1].label === c.label) return; // 지속은 병합
      points.push({ t: slotTime(c.bar * BS + (c.pos || 0)), label: c.label });
    });
    chordData = { points: points };
    document.getElementById('chord-label').hidden = false;
    document.getElementById('chord-strip').hidden = false;
    renderChordStrip();
  });

  function tickInterval(w) {
    var candidates = [1, 2, 5, 10, 15, 30, 60, 120];
    for (var i = 0; i < candidates.length; i++) {
      if (w / candidates[i] <= 9) return candidates[i];
    }
    return 240;
  }

  function drawRuler() {
    var svg = document.getElementById('ruler');
    var w = windowDur();
    var end = viewStart + w;
    svg.setAttribute('aria-label', '타임라인 눈금, ' + fmt(viewStart) + ' ~ ' + fmt(end));
    var major = tickInterval(w);
    var html = '';
    var t0 = Math.ceil(viewStart / major) * major;
    for (var t = t0 - major / 2; t < end; t += major) {
      if (t > viewStart) {
        var xm = (t - viewStart) / w * 600;
        html += '<line class="tick-minor" x1="' + xm.toFixed(1) + '" y1="18" x2="' + xm.toFixed(1) + '" y2="28"/>';
      }
    }
    for (var tm = t0; tm < end; tm += major) {
      var x = (tm - viewStart) / w * 600;
      html += '<line class="tick-major" x1="' + (x + 0.5).toFixed(1) + '" y1="12" x2="' + (x + 0.5).toFixed(1) + '" y2="28"/>';
      if (x < 600 - 42) { // 끝 라벨과 겹치면 눈금만 남기고 라벨 생략
        html += '<text class="tick-label" x="' + (x + 4).toFixed(1) + '" y="10">' + fmt(tm) + '</text>';
      }
    }
    html += '<text class="tick-label" x="596" y="10" text-anchor="end">' + fmt(end) + '</text>';
    svg.innerHTML = html;
  }

  /* ---- 줌 ---- */
  function setZoom(z) {
    z = Math.max(1, Math.min(MAX_ZOOM, z));
    if (z === zoom) return;
    var center = player.currentTime();
    zoom = z;
    viewStart = center - windowDur() / 2; // 재생 위치 중심으로 확대/축소
    clampView();
    document.getElementById('zoom-badge').textContent = '×' + zoom;
    drawWaves();
    state.zoom = zoom; saveState(); // 재진입 때 줌 유지(사용자 지적: x16 이 초기화됨)
  }

  /* ---- A-B 루프 영역 + 플레이헤드 (창 좌표계) — 버튼·플레이어 루프 적용은 transport 소유 ---- */
  var playhead = document.getElementById('playhead');
  var loopRegion = document.getElementById('loop-region');

  function pct(t) { return ((t - viewStart) / windowDur() * 100); }

  function renderOverlay() {
    var t = visualTime();
    var p = pct(t);
    playhead.style.left = Math.max(0, Math.min(100, p)) + '%';
    playhead.style.display = (p < 0 || p > 100) ? 'none' : '';

    var has = state.loopA != null && state.loopB != null;
    loopRegion.hidden = !has;
    if (has) {
      var a = Math.max(0, Math.min(100, pct(state.loopA)));
      var b = Math.max(0, Math.min(100, pct(state.loopB)));
      loopRegion.style.left = a + '%';
      loopRegion.style.width = Math.max(0, b - a) + '%';
    }
  }

  function seekAtClientX(el, clientX) {
    var rect = el.getBoundingClientRect();
    var frac = (clientX - rect.left) / rect.width;
    player.seek(viewStart + frac * windowDur());
    // 일시정지 중에도 시간·플레이헤드 즉시 갱신(onTick 은 재생 중에만 돈다)
    document.getElementById('time-now').textContent = fmt(player.currentTime());
    renderOverlay();
    saveState();
  }

  function renderToggles() {
    var solos = Array.isArray(state.solo) ? state.solo : (state.solo ? [state.solo] : []);
    document.querySelectorAll('[data-solo]').forEach(function (b) {
      var on = solos.indexOf(b.dataset.solo) >= 0; // 다중 솔로(합집합)
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', String(on));
    });
    document.querySelectorAll('[data-mute]').forEach(function (b) {
      var on = !!state.muted[b.dataset.mute];
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', String(on));
    });
  }

  // A-B 핸들 드래그 미세조정 (REQ-PLAY-004)
  function handleDrag(handleEl, which) {
    var overlay = document.getElementById('deck-overlay');
    handleEl.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      var rect = overlay.getBoundingClientRect();
      function move(ev) {
        var frac = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
        var t = viewStart + frac * windowDur();
        if (which === 'a' && (state.loopB == null || t < state.loopB)) state.loopA = t;
        if (which === 'b' && (state.loopA == null || t > state.loopA)) state.loopB = t;
        transport.renderLoopUI(); // 버튼 라벨·플레이어 루프까지 즉시 반영(→ loop 훅으로 오버레이·칩)
      }
      function up() {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        saveState();
      }
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    });
  }

  /* ---- 스피드 트레이너 (REQ-PLAY-011) — 반복이 끝날 때마다 배속 한 스텝 업 ---- */
  var trainer = null; // {target, step, reps, rep, prevT}

  function trainerStatus(msg) {
    document.getElementById('tr-status').textContent = msg || '';
  }
  function stopTrainer(msg) {
    trainer = null;
    document.getElementById('tr-go').hidden = false;
    document.getElementById('tr-stop').hidden = true;
    document.getElementById('btn-trainer').classList.remove('active');
    trainerStatus(msg || '');
  }
  function startTrainer() {
    if (state.loopA == null || state.loopB == null) {
      alert('먼저 A-B 반복 구간을 지정하거나 저장된 루프를 켜주세요');
      return;
    }
    var s = parseInt(document.getElementById('tr-start').value, 10) / 100;
    var g = parseInt(document.getElementById('tr-target').value, 10) / 100;
    var st = parseInt(document.getElementById('tr-step').value, 10) / 100;
    var n = parseInt(document.getElementById('tr-reps').value, 10);
    if (!(s > 0 && g > 0 && st > 0 && n > 0) || s >= g) {
      alert('시작 배속은 목표보다 낮아야 해요 (예: 70% → 100%)');
      return;
    }
    trainer = { target: g, step: st, reps: n, rep: 0, prevT: null };
    transport.applyRate(s);
    Shell.save({ rate: s });
    player.seek(state.loopA);
    if (!player.isPlaying()) { player.play(); transport.setPlayingUI(true); }
    document.getElementById('tr-go').hidden = true;
    document.getElementById('tr-stop').hidden = false;
    document.getElementById('btn-trainer').classList.add('active');
    trainerStatus('1/' + n + '회 · ' + Math.round(s * 100) + '%');
  }
  function trainerTick(t) {
    if (!trainer) return;
    if (state.loopA == null || state.loopB == null) {
      stopTrainer('반복 구간이 해제되어 멈췄어요');
      return;
    }
    // 루프 랩 감지: 시간이 구간 끝 근처에서 앞으로 되감김
    if (trainer.prevT != null && trainer.prevT - t > 0.5 && trainer.prevT > state.loopB - 2) {
      trainer.rep += 1;
      if (trainer.rep >= trainer.reps) {
        trainer.rep = 0;
        var next = Math.min(trainer.target, Math.round((player.rate + trainer.step) * 100) / 100);
        transport.applyRate(next);
        Shell.save({ rate: next });
        if (next >= trainer.target - 1e-9) {
          stopTrainer('목표 배속 도달! 잘하셨어요');
          return;
        }
      }
      trainerStatus((trainer.rep + 1) + '/' + trainer.reps + '회 · ' + Math.round(player.rate * 100) + '%');
    }
    trainer.prevT = t;
  }
  window.__trainer = function () { return trainer; }; // 배터리용

  /* ---- 저장된 루프 칩 (REQ-PLAY-010) ---- */
  function renderChips() {
    var chipRow = document.getElementById('loop-chips');
    var html = state.loops.map(function (lp, i) {
      return '<span class="loop-chip' + (state.activeLoop === i ? ' active' : '') + '" data-chip="' + i + '">' +
        lp.name + ' (' + fmt(lp.a) + '–' + fmt(lp.b) + ')' +
        '<button type="button" class="loop-chip-remove" data-chip-remove="' + i + '" aria-label="' + lp.name + ' 루프 삭제">×</button></span>';
    }).join('');
    html += '<button type="button" class="btn btn-outline btn-sm loop-chip-add" id="btn-loop-save">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>' +
      '루프 저장</button>';
    chipRow.innerHTML = html;
  }

  /* ---- 셸 훅 — 믹서 활성일 때만(숨은 파형 갱신은 헛일) ---- */
  Shell.on('tick', function () {
    // 플레이헤드 추종: 창 밖으로 나가면 창을 당겨온다 (DAW 표준).
    // 커서와 같은 시각(visualTime=소리 보정)으로 추종해야 커서가 창 안 제자리에 온다.
    var t = visualTime();
    if (zoom > 1 && (t < viewStart || t > viewStart + windowDur() * 0.97)) {
      viewStart = t - windowDur() * 0.05;
      clampView();
      drawWaves();
    } else {
      renderOverlay();
    }
    updateCurrentChord();
    updateNowLyric();
    trainerTick(t);
  }, 'mixer');
  Shell.on('seek', function () { renderOverlay(); updateCurrentChord(true); updateNowLyric(true); }, 'mixer');
  Shell.on('loop', function () { renderOverlay(); renderChips(); }, 'mixer');
  Shell.on('sync', function () { renderOverlay(); updateCurrentChord(true); updateNowLyric(true); }, 'mixer');

  /* ---- 뷰 모듈 등록 ---- */
  Shell.registerView('mixer', {
    init: function () {
      STEMS.forEach(stemRow);

      document.getElementById('zoom-in').addEventListener('click', function () { setZoom(zoom * 2); });
      document.getElementById('zoom-out').addEventListener('click', function () { setZoom(zoom / 2); });

      // 줌 상태에서 휠 = 좌우 이동 (한 칸당 창의 4% — 사용자 피드백: 10%는 너무 빠름).
      // ★파형 레인(과 룰러) 위에서만 좌우 이동. 솔로·음소거·세로 등 컨트롤 위에선 개입하지 않아
      // 평소처럼 세로 스크롤이 되게 한다(사용자 요청 2026-07-14).
      document.querySelector('.stem-deck').addEventListener('wheel', function (e) {
        if (zoom === 1) return;
        if (!e.target.closest('.stem-lane-cell') && !e.target.closest('.deck-ruler-cell')) return;
        e.preventDefault();
        viewStart += (e.deltaY / 100) * windowDur() * 0.04;
        clampView();
        drawWaves();
      }, { passive: false });

      /* 솔로/음소거/볼륨 */
      grid.addEventListener('click', function (e) {
        var soloBtn = e.target.closest('[data-solo]');
        var muteBtn = e.target.closest('[data-mute]');
        if (soloBtn) {
          state.solo = player.setSolo(soloBtn.dataset.solo);
          renderToggles();
          saveState();
        } else if (muteBtn) {
          var key = muteBtn.dataset.mute;
          state.muted[key] = !state.muted[key];
          player.setMute(key, state.muted[key]);
          renderToggles();
          saveState();
        } else {
          var vampBtn = e.target.closest('[data-vamp]');
          if (vampBtn) {
            var vk = vampBtn.dataset.vamp, cur = vamp[vk] || 1;
            vamp[vk] = cur >= 4 ? 1 : (cur >= 2 ? 4 : 2);  // 1→2→4→1 순환
            vampBtn.textContent = '세로 ' + vamp[vk] + '×';
            drawWaves();
          }
        }
      });
      grid.addEventListener('input', function (e) {
        var vol = e.target.closest('[data-volume]');
        if (vol) {
          state.volumes[vol.dataset.volume] = vol.value / 100;
          player.setStemVolume(vol.dataset.volume, vol.value / 100); // REQ-PLAY-005
          saveState();
        }
      });

      /* seek: 파형 클릭 + 룰러 클릭 (창 좌표계) */
      grid.addEventListener('click', function (e) {
        var lane = e.target.closest('.stem-lane-cell');
        if (lane) seekAtClientX(lane, e.clientX);
      });
      document.querySelector('.deck-ruler-cell').addEventListener('click', function (e) {
        seekAtClientX(e.currentTarget, e.clientX);
      });

      handleDrag(document.getElementById('handle-a'), 'a');
      handleDrag(document.getElementById('handle-b'), 'b');

      /* 루프 칩 행 */
      document.getElementById('loop-chips').addEventListener('click', function (e) {
        var rm = e.target.closest('[data-chip-remove]');
        if (rm) {
          var idx = parseInt(rm.dataset.chipRemove, 10);
          state.loops.splice(idx, 1);
          if (state.activeLoop === idx) state.activeLoop = null;
          renderChips(); saveState();
          return;
        }
        var chip = e.target.closest('[data-chip]');
        if (chip) {
          var i = parseInt(chip.dataset.chip, 10);
          state.activeLoop = i;
          state.loopA = state.loops[i].a;
          state.loopB = state.loops[i].b;
          transport.renderLoopUI(); saveState();
          return;
        }
        if (e.target.closest('#btn-loop-save')) {
          if (state.loopA == null || state.loopB == null) {
            alert('먼저 A 지점과 B 지점으로 반복 구간을 만들어주세요');
            return;
          }
          var name = prompt('이 구간의 이름을 지어주세요 (예: 1절 베이스라인)');
          if (!name) return;
          state.loops.push({ name: name, a: state.loopA, b: state.loopB });
          state.activeLoop = state.loops.length - 1;
          renderChips(); saveState();
        }
      });

      /* 구간 띠: 클릭 = 그 구간 A-B 반복, 더블클릭 = 이름 바꾸기 */
      document.getElementById('sect-strip').addEventListener('click', function (e) {
        var seg = e.target.closest('.sect-seg[data-i]');
        if (!seg || !sectionsData) return;
        var sec = sectionsData[parseInt(seg.dataset.i, 10)];
        state.loopA = sec.s;
        state.loopB = Math.min(sec.e, (player.duration() || sec.e) - 0.05);
        state.activeLoop = null;
        player.seek(sec.s);
        transport.renderLoopUI();
        saveState();
      });
      document.getElementById('sect-strip').addEventListener('dblclick', function (e) {
        var seg = e.target.closest('.sect-seg[data-i]');
        if (!seg || !sectionsData) return;
        var i = parseInt(seg.dataset.i, 10);
        var name = prompt('이 구간의 이름을 지어주세요 (예: 코러스, 2절)', sectionsData[i].name);
        if (!name || !name.trim()) return;
        fetch('/api/songs/' + Shell.songId + '/sections', {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ index: i, name: name.trim() }),
        }).then(function (r) {
          if (!r.ok) { alert('이름을 저장하지 못했어요'); return; }
          Shell.refreshMeta();
        });
      });

      /* 스피드 트레이너 */
      document.getElementById('btn-trainer').addEventListener('click', function () {
        var p = document.getElementById('trainer-panel');
        p.hidden = !p.hidden;
      });
      document.getElementById('tr-go').addEventListener('click', startTrainer);
      document.getElementById('tr-stop').addEventListener('click', function () { stopTrainer(''); });

      /* 화면 고유 단축키: 1~6 스템 솔로 (공통 키는 transport.js) */
      document.addEventListener('keydown', function (e) {
        var tag = (e.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select' || tag === 'button') return;
        if (e.key >= '1' && e.key <= '6') {
          var stem = STEMS[parseInt(e.key, 10) - 1];
          state.solo = player.setSolo(stem.key);
          renderToggles(); saveState();
        }
      });

      // 파형 데이터 + 셸 준비(상태·스템) → 첫 렌더
      Promise.all([
        fetch('/api/songs/' + Shell.songId + '/peaks').then(function (r) { return r.json(); }),
        Shell.ready,
      ]).then(function (res) {
        peaksData = res[0];
        // 슬라이더·토글을 저장 상태로 (플레이어 반영은 셸이 이미)
        Object.keys(state.volumes || {}).forEach(function (k) {
          var slider = document.querySelector('[data-volume="' + k + '"]');
          if (slider) slider.value = state.volumes[k] * 100;
        });
        renderToggles();
        renderChips();
        if (state.zoom && state.zoom > 1) { // 줌 상태 복원(재진입 초기화 방지, 사용자 지적)
          zoom = Math.max(1, Math.min(MAX_ZOOM, state.zoom));
          document.getElementById('zoom-badge').textContent = '×' + zoom;
          viewStart = (player.currentTime() || 0) - windowDur() / 2;
          clampView();
        }
        drawWaves();
        window.__practiceReady = true;
      });
    },
    activate: function () { // 숨김 동안 밀린 갱신 — 상태(A-B·위치·코드)는 이미 공유 객체에
      if (window.__practiceReady) { renderChips(); drawWaves(); }
    },
  });

  window.__view = function () { return { zoom: zoom, viewStart: viewStart, windowDur: windowDur() }; };
})();
