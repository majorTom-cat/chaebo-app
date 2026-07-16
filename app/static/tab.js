/* 타브 뷰 — 시안 05 충실 구현.
   렌더: 실제 alphaTab(vendor) 조판 + boundsLookup 오버레이(커서·낮은확신 점·보정 팝오버).
   재생: SyncPlayer(불변식) — 커서는 외부 클록(재생 시간)→tick→비트 좌표 (SP-4 프로토 검증 패턴).
   플레이어·트랜스포트·상태·저장은 셸(shell.js)이 문서당 1개 소유 — 여기는 조판·커서·편집만. */
(function () {
  'use strict';

  var songId = Shell.songId;
  var player = Shell.player;
  var sharedState = Shell.state; // 살아있는 공유 객체 — 재할당 금지
  var transport = Shell.transport;
  var fmt = Shell.fmt;
  function saveShared(patch) { Shell.save(patch); }

  var api = null;          // alphaTab
  var tab = null;          // {bpm, notes, tex, ...}
  var beats = [];          // [{startSec, x, y, w, h, barX, barW, barY, barH}]
  var barBounds = {};      // barIndex -> {x,w,y,h} — 커서의 마디 내 시간 비례 이동용
  var tabBotByRowY = {};   // 줄의 스코어 보표 y -> 타브 보표 하단 y (카운트 행 = 타브 아래)
  var tabMidByRowY = {};   // 줄의 스코어 보표 y -> 타브 보표 중앙 y (편집 고스트 점 앵커)
  var duration = 0;

  /* ---- 셸 훅 — 타브 활성일 때만(숨은 커서 갱신은 헛일) ---- */
  Shell.on('tick', function () { updateCursor(); }, 'tab');
  Shell.on('seek', function () { updateCursor(); }, 'tab');
  Shell.on('sync', function () { updateCursor(); }, 'tab');
  Shell.on('play', function (playing) {
    document.getElementById('playhead-caption').hidden = !playing;
  }, 'tab');
  // 다른 뷰發 변경(키 직접 입력·코드/가사 수정) 반영 — 조판·가사가 낡은 채 남지 않게.
  // 편집 저장과의 짧은 경합은 자가 치유: saveNotes 응답이 최신 tex 로 다시 그린다
  Shell.on('meta', function (t) {
    if (!tab || t.status !== 'ready') return;
    if (t.lyrics && JSON.stringify(t.lyrics) !== JSON.stringify(tab.lyrics)) {
      tab.lyrics = t.lyrics; // 가사만 바뀜(tex 불변) — 오버레이만 다시
      placeLyrics();
    }
    if (!t.tex || t.tex === tab.tex) return;
    tab = t;
    showBpm();
    renderFlow();
    if (Shell.active() === 'tab') { freezeScore(); renderScore(); }
    else needsScoreRender = true;
  });

  /* ---- 타브 상태 머신: none/queued/analyzing → ready ---- */
  var emptyBox = document.getElementById('tab-empty');
  var readyBox = document.getElementById('tab-ready');
  var pollTimer = null;

  function refreshTab() {
    lastEditGi = null; // 전체 재계산(박자 시작점·박자 전환 등) — 칸 좌표가 통째로 바뀌어 '최근 수정' 표시 무효
    fetch('/api/songs/' + songId + '/tab').then(function (r) { return r.json(); }).then(function (t) {
      if (t.status === 'ready') {
        clearInterval(pollTimer);
        pollTimer = null;
        tab = t;
        emptyBox.hidden = true;
        readyBox.hidden = false;
        showBpm();
        document.getElementById('meter-label').textContent = t.meter || '4/4';
        // 분석 설정(감도·빠르기·음정·박자엔진·첫음정박)은 '다시 분석' 다이얼로그에서 현재값으로 프리필(아래).
        transport.setMeta(t); // 메트로놈·카운트인 활성화(재분석 직후에도 신선하게)
        renderFlow(); // 고정 px 좌표 — 숨김 중에도 안전
        if (Shell.active() === 'tab') renderScore();
        else needsScoreRender = true; // 숨김 중 조판은 컨테이너 폭 0 — 활성화 때로 미룸
      } else if (t.status === 'queued' || t.status === 'analyzing') {
        emptyBox.hidden = false;
        readyBox.hidden = true;
        document.getElementById('tab-empty-msg').textContent =
          '베이스 소리에서 음을 따는 중이에요… (' + Math.round(t.progress || 0) + '%)';
        document.getElementById('tab-progress-wrap').hidden = false;
        document.getElementById('tab-progress').style.width = (t.progress || 0) + '%';
        document.getElementById('btn-make-tab').hidden = true;
        if (!pollTimer) pollTimer = setInterval(refreshTab, 3000);
      } else if (t.status === 'error') {
        clearInterval(pollTimer);
        pollTimer = null;
        emptyBox.hidden = false;
        readyBox.hidden = true;
        document.getElementById('tab-empty-msg').textContent = t.error || '분석에 실패했어요';
        document.getElementById('tab-progress-wrap').hidden = true;
        document.getElementById('btn-make-tab').hidden = false;
        document.getElementById('btn-make-tab').textContent = '다시 시도';
      } else {
        emptyBox.hidden = false;
        readyBox.hidden = true;
      }
    });
  }

  function startTab() {
    fetch('/api/songs/' + songId + '/tab', { method: 'POST' }).then(refreshTab);
  }
  document.getElementById('btn-make-tab').addEventListener('click', startTab);
  // '다시 분석' → 방법 선택 다이얼로그를 연다(사용자 요청: 모델 선택창). 현재 저장값으로 프리필.
  function amSetRadio(name, val) {
    var el = document.querySelector('input[name="' + name + '"][value="' + val + '"]');
    if (el) el.checked = true;
  }
  function amGetRadio(name) {
    var el = document.querySelector('input[name="' + name + '"]:checked');
    return el ? el.value : null;
  }
  function openAnalyzeDialog() {
    var t = tab || {};
    amSetRadio('am-beat', t.beat_engine || 'beat_track');   // 기본=고른 박자
    amSetRadio('am-detect', t.detect_engine || 'onset');    // 권장=픽 기반(어택=음 1:1). NULL(옛 bp곡)도 onset 권장 표시
    amSetRadio('am-sens', t.sensitivity || 'normal');
    amSetRadio('am-precision', t.crepe_mode || 'tiny');
    amSetRadio('am-tempo', t.tempo_override || 'auto');
    document.getElementById('am-lead-snap').checked = (t.lead_snap === 1);
    document.getElementById('analyze-modal').hidden = false;
  }
  function closeAnalyzeDialog() { document.getElementById('analyze-modal').hidden = true; }
  document.getElementById('btn-reanalyze').addEventListener('click', openAnalyzeDialog);
  document.getElementById('am-cancel').addEventListener('click', closeAnalyzeDialog);
  document.getElementById('am-close').addEventListener('click', closeAnalyzeDialog);
  document.getElementById('analyze-modal').addEventListener('click', function (e) {
    if (e.target.id === 'analyze-modal') closeAnalyzeDialog(); // 바깥 클릭 닫기
  });
  document.getElementById('am-go').addEventListener('click', function () {
    var body = {
      beat_engine: amGetRadio('am-beat'),          // 'beat_track'|'plp'|'beat_this'
      detect_engine: amGetRadio('am-detect'),      // 'bp'|'f0' — 음 검출 방식
      sensitivity: amGetRadio('am-sens'),          // 'normal'|'simple'
      mode: amGetRadio('am-precision'),            // 'tiny'|'full'
      tempo: amGetRadio('am-tempo'),               // 'auto'|'half'|'double'
      lead_snap: document.getElementById('am-lead-snap').checked,
    };
    closeAnalyzeDialog();
    fetch('/api/songs/' + songId + '/tab', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(refreshTab);
  });

  // 박자 시작점 이동 — 1에서 시작하지 않는 곡의 수동 맞춤
  function shiftPhase(slots, beat) {
    fetch('/api/songs/' + songId + '/tab/shift', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slots: slots, beat: !!beat }),
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (e) { alert(e.detail || '옮길 수 없어요'); });
      refreshTab();
    });
  }
  document.getElementById('btn-shift-left').addEventListener('click', function () { shiftPhase(-1); });
  document.getElementById('btn-shift-right').addEventListener('click', function () { shiftPhase(1); });
  document.getElementById('btn-beat-left').addEventListener('click', function () { shiftPhase(-1, true); });
  document.getElementById('btn-beat-right').addEventListener('click', function () { shiftPhase(1, true); });

  // 박자 수동 전환(4/4⇄12/8) — 자동 판정 오판 대비, 캐시 재계산이라 몇 초
  document.getElementById('btn-meter').addEventListener('click', function () {
    if (!tab) return;
    var next = tab.meter === '12/8' ? '4/4' : '12/8';
    if (!confirm('박자를 ' + next + ' 로 바꿔 다시 계산할까요? 직접 고친 음은 사라져요')) return;
    fetch('/api/songs/' + songId + '/tab', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meter: next }),
    }).then(refreshTab);
  });

  /* ---- 흐름 타브(시간 비례 뷰) — 칸=고정 폭: 간격 균일·커서 등속·일렬 정렬이 구조적으로 보장 ---- */
  var SLOT_PX = 22;   // 16분음표 한 칸 폭
  var FLOW_PAD = 12;

  function flowX(g) { return FLOW_PAD + g * subPx(); }

  // 그리드: 4/4 v1 = 마디 16슬롯(슬롯=16분) · 4/4 v2 = 48슬롯(박당 12 — 16분·셋잇단 공존)
  //        · 12/8 = 48슬롯(슬롯=펄스의 1/4, 펄스=8분)
  function barSlots() { return (tab && tab.bar_slots) || 16; }
  function isCompound() { return !!tab && tab.meter === '12/8'; }
  function isMixed() { return !!tab && tab.meter !== '12/8' && barSlots() === 48; }
  function subPx() { // 슬롯 하나의 픽셀 폭 — 16분 상당이 항상 22px
    return isMixed() ? SLOT_PX / 3 : SLOT_PX;
  }
  var ALLOWED_48 = [0, 3, 4, 6, 8, 9]; // 박(12칸) 내 허용 위치: 16분 + 셋잇단
  function snapAllowed(sub) { // 4/4 v2: 임의 슬롯 → 가장 가까운 허용 위치
    if (!isMixed()) return Math.max(0, sub);
    var beat = Math.floor(sub / 12);
    var best = null;
    [beat - 1, beat, beat + 1].forEach(function (b) {
      if (b < 0) return;
      ALLOWED_48.forEach(function (o) {
        var g = b * 12 + o;
        if (best === null || Math.abs(g - sub) < Math.abs(best - sub)) best = g;
      });
    });
    return best;
  }
  function sylFor(s) { // s = 마디 내 슬롯 → 카운트 음절('' = 라벨 없는 칸)
    if (isCompound()) {
      if (s % 4 !== 0) return '';
      var p = s / 4; // 펄스 0..11
      return p % 3 === 0 ? String(p / 3 + 1) : (p % 3 === 1 ? 'la' : 'li');
    }
    if (isMixed()) {
      var o = s % 12;
      if (o === 0) return String(Math.floor(s / 12) + 1);
      return { 3: 'e', 6: 'n', 9: 'a', 4: 'la', 8: 'li' }[o] || '';
    }
    return COUNT_SYL[s % 16];
  }

  // 슬롯↔시각 변환 — 동적 그리드(slots: 실연주 템포 추종)가 있으면 그것을, 없으면 균일 그리드
  // 균일 폴백 한 칸 = 혼합(박당 12칸) 60/bpm/12 · 그 외 60/bpm/4 — v1 상수 하드코딩이
  // 48그리드+slots 부재 곡에서 커서·클릭 시간을 3배 어긋내던 결함(배터리 실증 2026-07-09)
  function uSlotDur() {
    return 60 / tab.bpm / (isMixed() ? 12 : 4);
  }
  function slotTime(g) {
    if (tab && tab.slots && tab.slots.length > 1) {
      var i = Math.max(0, Math.min(tab.slots.length - 2, Math.floor(g)));
      return tab.slots[i] + (tab.slots[i + 1] - tab.slots[i]) * (g - i);
    }
    return (tab.offset || 0) + g * uSlotDur();
  }
  function timeSlot(t) {
    if (tab && tab.slots && tab.slots.length > 1) {
      var s = tab.slots, lo = 0, hi = s.length - 1;
      while (lo < hi) { var mid = (lo + hi + 1) >> 1; if (s[mid] <= t) lo = mid; else hi = mid - 1; }
      var i = Math.min(lo, s.length - 2);
      var span = s[i + 1] - s[i] || 1;
      return i + (t - s[i]) / span;
    }
    return (t - (tab.offset || 0)) / uSlotDur();
  }

  function renderFlow() {
    var inner = document.getElementById('flow-inner');
    inner.querySelectorAll('.flow-bar-line, .flow-bar-num, .flow-count, .flow-string, .flow-fret, .flow-sustain, .flow-chord')
      .forEach(function (el) { el.remove(); });
    if (!tab || !tab.notes || !tab.notes.length) return;

    var attack = {}, byG = {};
    tab.notes.forEach(function (nt) { attack[nt.gi] = true; byG[nt.gi] = nt; });
    var BAR = barSlots();
    var lastEnd = 0;
    tab.notes.forEach(function (nt) { lastEnd = Math.max(lastEnd, nt.gi + nt.glen); });
    var totalBars = Math.ceil(lastEnd / BAR) + 1; // 여분 1마디 — 곡 끝 뒤에도 음 추가 가능(편집)
    inner.style.width = (FLOW_PAD * 2 + totalBars * BAR * subPx()) + 'px';

    var frag = document.createDocumentFragment();
    // 현 4줄 (위=G · 아래=E, 타브 관례) — 맨 위 18px 는 코드 행
    for (var li = 0; li < 4; li++) {
      var line = document.createElement('div');
      line.className = 'flow-string';
      line.style.top = (70 + li * 24) + 'px';
      frag.appendChild(line);
    }
    // 코드(추정) — 제 위치(반마디 코드는 마디 중간). 직전과 같으면 흐리게(변화 지점이 잘 보이게)
    var chSorted = (tab.chords || []).slice().sort(function (a, b) {
      return (a.bar - b.bar) || ((a.pos || 0) - (b.pos || 0));
    });
    chSorted.forEach(function (ch, ci) {
      var el = document.createElement('div');
      var same = ci > 0 && chSorted[ci - 1].label === ch.label;
      el.className = 'flow-chord' + (same ? ' fc-same' : '');
      el.textContent = ch.label;
      el.style.left = (flowX(ch.bar * BAR + (ch.pos || 0)) + 4) + 'px';
      frag.appendChild(el);
    });
    var anchorSet = {};
    (tab.anchors || []).forEach(function (g) { anchorSet[g] = 1; });
    for (var bi = 0; bi <= totalBars; bi++) {
      var giBar = bi * BAR;
      var anchored = !!anchorSet[giBar];
      var bl = document.createElement('div');
      bl.className = 'flow-bar-line' + (anchored ? ' fbl-anchored' : '');
      bl.style.left = flowX(giBar) + 'px';
      frag.appendChild(bl);
      if (bi < totalBars) {
        var bn = document.createElement('div');
        bn.className = 'flow-bar-num';
        bn.textContent = bi + 1;
        bn.style.left = (flowX(giBar) + 4) + 'px';
        frag.appendChild(bn);
        // 드래그 그립 — 이 마디 시작을 실제 박(파형 어택)으로 끌어 격자 워프. 첫 마디(gi0)는 시작 고정점이라 제외.
        if (bi > 0) {
          var grip = document.createElement('div');
          grip.className = 'flow-bar-grip' + (anchored ? ' fbg-anchored' : '');
          grip.style.left = flowX(giBar) + 'px';
          grip.dataset.gi = giBar;
          grip.title = anchored ? '박자 앵커 — 다시 끌어 옮기거나 아래 ‘박자 앵커 지우기’ 로 해제'
            : '끌어서 이 마디를 실제 박(파형 어택선)에 맞추기';
          frag.appendChild(grip);
        }
      }
      if (bi === totalBars) break;
      // 카운트 — 4/4: 적응형 세분(어택 있는 세분만 확장) · 12/8: 펄스마다 1 la li(사용자 확정 표기)
      var bases = [];
      if (isCompound()) {
        for (var p = 0; p < 12; p++) bases.push({ g: bi * BAR + p * 4, on: p % 3 === 0 });
      } else if (isMixed()) {
        for (var q2 = 0; q2 < 4; q2++) {
          var b2 = bi * BAR + q2 * 12;
          bases.push({ g: b2, on: true });
          if (attack[b2 + 3] || attack[b2 + 9]) { // 16분 어택 있는 박 → e·a
            bases.push({ g: b2 + 3, on: false });
            bases.push({ g: b2 + 9, on: false });
          }
          if (attack[b2 + 4] || attack[b2 + 8]) { // 셋잇단 어택 있는 박 → la·li
            bases.push({ g: b2 + 4, on: false });
            bases.push({ g: b2 + 8, on: false });
          }
          if (!(attack[b2 + 4] || attack[b2 + 8])) bases.push({ g: b2 + 6, on: false }); // n
        }
        bases.sort(function (a, b3) { return a.g - b3.g; });
      } else {
        for (var q = 0; q < 4; q++) {
          var base = bi * BAR + q * 4;
          var subdivided = attack[base + 1] || attack[base + 3];
          [0, 1, 2, 3].forEach(function (j) {
            if ((j === 1 || j === 3) && !subdivided) return;
            bases.push({ g: base + j, on: j === 0 });
          });
        }
      }
      bases.forEach(function (b) {
        var g = b.g;
        var c = document.createElement('div');
        var isAtk = !!attack[g];
        c.className = 'flow-count' + (b.on ? ' fc-onbeat' : '') + (isAtk ? ' fc-attack' : ' fc-quiet');
        c.textContent = sylFor(g % BAR);
        c.dataset.g = g;
        c.style.left = flowX(g) + 'px';
        frag.appendChild(c);
      });
    }
    tab.notes.forEach(function (nt) {
      var y = 70 + (3 - nt.string) * 24;
      if (nt.glen > 1) {
        var su = document.createElement('div');
        su.className = 'flow-sustain';
        su.style.left = flowX(nt.gi) + 'px';
        su.dataset.gx = flowX(nt.gi);   // 격자 원위치(스냅 멱등용)
        su.style.width = (nt.glen * subPx() - 6) + 'px';
        su.style.top = y + 'px';
        frag.appendChild(su);
      }
      var f = document.createElement('div');
      f.className = 'flow-fret' + (nt.conf < CONF_TH ? ' fc-low' : '');
      f.textContent = nt.fret;
      f.style.left = flowX(nt.gi) + 'px';
      f.dataset.gx = flowX(nt.gi);      // 격자 원위치(스냅 멱등용)
      f.style.top = y + 'px';
      frag.appendChild(f);
    });
    inner.appendChild(frag);
    window.__flowReady = true;
    drawFlowWave(); // 베이스 파형 띠 — 폭 확정 뒤(같은 grid 축)
    renderGutter(); // 좌측 고정 줄이름 라벨(스크롤 무관)
  }

  // 좌측 고정 칸(gutter) — 각 가로줄 의미를 항상 좌측에 표시(사용자 요청 2026-07-14: 스크롤 옮겨도 유지).
  // 현 라벨 y 는 renderFlow 의 현 위치(70 + li·24)와 동일해 정확히 정렬. flow-scroll 밖이라 안 밀린다.
  function renderGutter() {
    var gut = document.getElementById('flow-gutter');
    if (!gut) return;
    gut.innerHTML = '';
    var names = ['G', 'D', 'A', 'E']; // 위=G(얇은 줄) ~ 아래=E(굵은 줄), 타브 관례
    for (var i = 0; i < 4; i++) {
      var s = document.createElement('div');
      s.className = 'g-str';
      s.textContent = names[i];
      s.title = names[i] + '현 (' + (i === 0 ? '가장 얇은 줄' : i === 3 ? '가장 굵은 줄' : '') + ')';
      s.style.top = (70 + i * 24) + 'px';
      gut.appendChild(s);
    }
    var note = function (top, txt) {
      var d = document.createElement('div');
      d.className = 'g-note'; d.style.top = top + 'px'; d.textContent = txt;
      gut.appendChild(d);
    };
    note(9, '코드');
    note(41, '박자');
    note(183, '베이스');  // 파형 위 띠(158~208 중심)
    note(233, '드럼');    // 파형 아래 띠(208~258 중심)
  }

  // 베이스 파형 띠(사용자 요청 2026-07-14): 믹서와 같은 peaks 를, 흐름 타브의 칸(grid) 축에 매핑해
  // 그린다. x→g→시간은 커서와 동일한 slotTime — 그래서 진행바 하나가 파형·음표를 같은 x 에서 관통하고
  // 어택이 바로 위 음표와 세로로 맞는다. peaks 는 곡 [0,duration] 전구간 envelope(v5, 늘어남 버그 없음).
  var FLOW_WAVE_H = 100;  // 위 50px 베이스 + 아래 50px 드럼(사용자: 각각 제 크기로·겹치지 말고)
  function drawFlowWave() {
    var svg = document.getElementById('flow-wave');
    var inner = document.getElementById('flow-inner');
    var bass = (window.__peaks && window.__peaks.bass) || null;
    var W = inner ? (parseFloat(inner.style.width) || inner.offsetWidth) : 0;
    if (!svg || !W || !bass || !bass.length || !tab || !tab.bpm) { if (svg) svg.innerHTML = ''; return; }
    var dur = (player && player.duration && player.duration()) || 0;
    if (!dur) return; // 오디오 로드 전 — peaks 훅/틱에서 다시 그림
    var step = Math.max(2, Math.round(W / 5000)); // 포인트 상한(긴 곡도 SVG 1회 렌더 가볍게)
    // 한 스템(peaks)을 같은 시간축(slotTime)으로, 지정 띠(midY 중심·halfH 반높이)에 폴리곤으로 — 각자 최대 정규화.
    function polyFor(peaks, midY, halfH) {
      var n = peaks.length, pmax = 0;
      for (var m = 0; m < n; m++) { if (peaks[m] > pmax) pmax = peaks[m]; }
      if (!pmax) pmax = 1;
      var top = [], bot = [];
      for (var x = FLOW_PAD; x <= W - FLOW_PAD; x += step) {
        var t0 = slotTime((x - FLOW_PAD) / subPx());
        var t1 = slotTime((x + step - FLOW_PAD) / subPx());
        var lo = Math.min(t0, t1), hi = Math.max(t0, t1);
        var i0 = Math.max(0, Math.floor(lo / dur * n));
        var i1 = Math.min(n, Math.max(i0 + 1, Math.ceil(hi / dur * n)));
        var v = 0;
        for (var j = i0; j < i1; j++) { if (peaks[j] > v) v = peaks[j]; } // 구간 max — 어택 보존
        var h = Math.max(0.4, Math.min(halfH - 0.4, (v / pmax) * (halfH - 1)));
        top.push(x + ',' + (midY - h).toFixed(1));
        bot.push(x + ',' + (midY + h).toFixed(1));
      }
      bot.reverse();
      return top.join(' ') + ' ' + bot.join(' ');
    }
    svg.setAttribute('width', W);
    svg.setAttribute('height', FLOW_WAVE_H);
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + FLOW_WAVE_H);
    // ★베이스(위 띠)·드럼(아래 띠)을 분리해서(사용자 요청 2026-07-15: 겹치면 둘 다 안 보임 — 아래에 구분).
    // 같은 시간축이라 위·아래 어택이 세로로 나란하면 베이스가 드럼(킥)에 맞춰 친 것. 가운데 구분선.
    var H = FLOW_WAVE_H, hb = H / 4 - 1;  // 각 띠 반높이(100 → ~24, 원래 50px 진폭 복원)
    var html = '<polygon class="wb" points="' + polyFor(bass, H / 4, hb) + '"/>';  // 베이스 = 위 띠(믹서 베이스 색)
    var drums = (window.__peaks && window.__peaks.drums) || null;
    if (drums && drums.length) {
      html += '<polygon class="wd" points="' + polyFor(drums, H * 3 / 4, hb) + '"/>';  // 드럼 = 아래 띠(믹서 드럼 색)
    }
    // (베이스/드럼 사이 가로 구분선 제거 — 사용자 지적 2026-07-16: 안 쓰이는 가로선. 색으로 이미 구분됨.)
    // ★오른손 어택 표시 — 음(픽)마다 세로선 1개를 그 음 위치(flowX(gi))에 그린다. onset 기반 검출에선
    //   '픽 하나 = 음 하나'라 어택선 개수 = 타브 개수(사용자 지적 2026-07-16: 둘은 같은 사건, 개수가 같아야).
    //   선이 프렛과 정확히 같은 x → '어택=타브 위치'. 높이 = 그 순간 파형 진폭(세게 친 만큼 길다).
    if (bass && bass.length && dur && tab && tab.notes) {
      var bmax = 0;
      for (var bk = 0; bk < bass.length; bk++) if (bass[bk] > bmax) bmax = bass[bk];
      if (bmax > 0) {
        var n2 = bass.length, atk = '';
        var span = Math.max(1, Math.round(0.03 * n2 / dur));  // 진폭 표본 ±30ms
        for (var ni = 0; ni < tab.notes.length; ni++) {
          var nt = tab.notes[ni];
          var xg = flowX(nt.gi);
          if (xg < FLOW_PAD || xg > W - FLOW_PAD) continue;
          var idx = Math.round(slotTime(nt.gi) / dur * n2);   // 그 음 시각의 파형 표본 위치
          var av = 0;
          for (var q = Math.max(0, idx - span); q < Math.min(n2, idx + span); q++) if (bass[q] > av) av = bass[q];
          var h = Math.max(1.5, Math.min(hb - 0.4, (av / bmax) * (hb - 1)));
          atk += '<line class="wa-atk" x1="' + xg.toFixed(1) + '" y1="' + (H / 4 - h).toFixed(1) +
            '" x2="' + xg.toFixed(1) + '" y2="' + (H / 4 + h).toFixed(1) + '"/>';
        }
        html += atk;
      }
    }
    svg.innerHTML = html;
  }

  window.__drawFlowWave = drawFlowWave; // 믹서 페이지는 practice.js 가 __peaks 를 채운 뒤 이걸 호출

  // 타브 페이지는 믹서 뷰 init 이 안 돌아 __peaks 가 비어 있다(실측). 여기서 직접 받아 그린다(같은
  // 캐시 엔드포인트라 서버 부담 0, 중복은 __peaks 존재로 방지). duration 은 스템 로드 뒤라 Shell.ready 후.
  var _peaksTried = false;
  function ensureBassPeaks() {
    if (window.__peaks && window.__peaks.bass) { drawFlowWave(); return; }
    if (_peaksTried || document.body.dataset.view !== 'tab') return; // 믹서 뷰는 practice.js 담당
    _peaksTried = true;
    fetch('/api/songs/' + songId + '/peaks').then(function (r) { return r.json(); }).then(function (d) {
      window.__peaks = d;
      drawFlowWave();
    }).catch(function () { _peaksTried = false; });
  }
  if (window.Shell && Shell.ready && Shell.ready.then) Shell.ready.then(function () { ensureBassPeaks(); });

  function updateFlowCursor(t) {
    if (!tab || !tab.bpm) return;
    var x = FLOW_PAD + timeSlot(t) * subPx(); // 박 단위 등속 — 동적 그리드면 실연주 박을 추종
    var ph = document.getElementById('flow-playhead');
    ph.style.left = Math.max(0, x) + 'px';
    var sc = document.getElementById('flow-scroll');
    if (x < sc.scrollLeft + 60 || x > sc.scrollLeft + sc.clientWidth - 120) {
      sc.scrollLeft = Math.max(0, x - 160);
    }
  }

  document.getElementById('flow-inner').addEventListener('click', function (e) {
    if (!tab || !tab.bpm) return;
    if (e.target.closest('.correction-popover')) return; // 팝오버 내부 클릭은 통과
    if (e.target.closest('.flow-bar-grip')) return; // 박자 앵커 그립은 드래그 전용 — seek 안 함
    var rect = document.getElementById('flow-inner').getBoundingClientRect();
    if (editMode) {
      var gi = snapAllowed(Math.round((e.clientX - rect.left - FLOW_PAD) / subPx()));
      if (gi < 0) return;
      var hit = -1;
      tab.notes.forEach(function (n, i) { if (n.gi === gi) hit = i; });
      if (hit >= 0) { openPopoverFlow(hit); return; }
      // 빈 칸: 클릭한 줄(현) 위에 새 음표 — 개방현으로 넣고 팝오버로 음 높이 조정.
      // 판정은 관대하게: 카운트 행(위 55px) 아래면 가장 가까운 현으로(±12px 요구는 실패 잦음 — 실증)
      if (e.clientY - rect.top < 55) return; // 코드·카운트 행 클릭은 무시
      var li = Math.max(0, Math.min(3, Math.round((e.clientY - rect.top - 70) / 24)));
      addNoteAt(gi, 3 - li);
      return;
    }
    var t = slotTime((e.clientX - rect.left - FLOW_PAD) / subPx());
    player.seek(Math.max(0, Math.min(duration || player.duration() || 0, t)));
    document.getElementById('time-now').textContent = fmt(player.currentTime());
    updateCursor();
  });

  /* 수동 박자 앵커(드래그) — 마디선 그립을 잡아 파형의 실제 박(어택선) 위로 끌면 그 마디 시작을 그 오디오
     시각에 고정하고 격자를 구간별 워프(가변 템포 잔여 드리프트 보정). gi 불변 → 표기·코드 그대로, 파형·커서만 맞춰짐. */
  function postAnchor(body) {
    return fetch('/api/songs/' + songId + '/tab/anchor', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || '앵커 실패'); });
      return r.json();
    }).then(function () { refreshTab(); }).catch(function (err) { alert(err.message); });
  }
  var _anchorDrag = null;
  document.getElementById('flow-inner').addEventListener('mousedown', function (e) {
    var grip = e.target.closest('.flow-bar-grip');
    if (!grip || !tab) return;
    e.preventDefault();
    var inner = document.getElementById('flow-inner');
    var ghost = document.createElement('div');
    ghost.className = 'flow-anchor-ghost';
    ghost.style.left = flowX(+grip.dataset.gi) + 'px';
    inner.appendChild(ghost);
    _anchorDrag = { gi: +grip.dataset.gi, startX: flowX(+grip.dataset.gi), ghost: ghost, inner: inner };
    document.body.classList.add('anchoring');
  });
  document.addEventListener('mousemove', function (e) {
    if (!_anchorDrag) return;
    var rect = _anchorDrag.inner.getBoundingClientRect();
    var x = Math.max(FLOW_PAD, e.clientX - rect.left);
    _anchorDrag.ghost.style.left = x + 'px';
  });
  document.addEventListener('mouseup', function (e) {
    if (!_anchorDrag) return;
    var d = _anchorDrag; _anchorDrag = null;
    document.body.classList.remove('anchoring');
    var rect = d.inner.getBoundingClientRect();
    var x = Math.max(FLOW_PAD, e.clientX - rect.left);
    d.ghost.remove();
    if (Math.abs(x - d.startX) < 4) return; // 거의 안 움직였으면 취소(오조작 방지)
    var t = slotTime((x - FLOW_PAD) / subPx()); // 드롭 지점의 실제 오디오 시각
    postAnchor({ gi: d.gi, t: Math.round(t * 1000) / 1000 });
  });
  var _clr = document.getElementById('btn-anchor-clear');
  if (_clr) _clr.addEventListener('click', function () {
    if (tab && tab.anchors && tab.anchors.length && confirm('박자 앵커를 모두 지우고 자동 격자로 되돌릴까요?')) {
      postAnchor({ clear: true });
    }
  });

  /* 재조판 동안 옛 조판을 그대로 덮어둠 — 진행형 재렌더가 화면을 꿀렁이게 하던 것 차단.
     v2: svg 깊은 복제(cloneNode)는 대형 악보에서 메인 스레드를 수백 ms 잠갔음 →
     옛 #alphaTab 요소를 통째로 덮개로 전환하고 새 렌더는 새 요소에(복제 비용 0).
     옛 api 파괴는 덮개 제거 시점으로 미룸(먼저 부수면 옛 화면이 지워짐). */
  var frozenApi = null;
  var freezeTimer = null;
  function freezeScore() {
    if (document.getElementById('score-freeze')) return;
    var el = document.getElementById('alphaTab');
    if (!el.offsetHeight) return;
    var h = el.offsetHeight;
    var box = { left: el.offsetLeft, top: el.offsetTop, width: el.offsetWidth }; // fresh 삽입 전에 캡처
    var fresh = document.createElement('div');
    fresh.style.minHeight = h + 'px';
    el.parentElement.insertBefore(fresh, el);
    el.id = 'score-freeze';
    el.style.cssText = 'position:absolute;left:' + box.left + 'px;top:' + box.top +
      'px;width:' + box.width + 'px;height:' + h + 'px;overflow:hidden;pointer-events:none;';
    fresh.id = 'alphaTab';
    frozenApi = api;
    api = null; // renderScore 가 새 요소에 새 api 를 만든다
    clearTimeout(freezeTimer);
    freezeTimer = setTimeout(unfreezeScore, 8000); // 렌더 실패 안전핀(이전 사이클 타이머는 위에서 취소)
  }
  function unfreezeScore() {
    clearTimeout(freezeTimer);  // 빠른 연속 편집 시 옛 타이머가 다음 사이클을 잘못 해제하던 것 방지(리뷰 2026-07-14)
    var g = document.getElementById('score-freeze');
    if (g) g.remove();
    if (frozenApi) {
      try { frozenApi.destroy(); } catch (e) { /* 이미 제거된 DOM — 무해 */ }
      frozenApi = null;
    }
  }

  /* ---- alphaTab 렌더 + boundsLookup 수집 ---- */
  function renderScore() {
    var el = document.getElementById('alphaTab');
    // 재조판 동안 높이 고정 — 비우는 순간 페이지가 접혀 스크롤이 맨 위로 튐(사용자 실증 2회)
    if (el.offsetHeight > 0) el.style.minHeight = el.offsetHeight + 'px';
    el.innerHTML = '';
    if (api) api.destroy();
    api = new alphaTab.AlphaTabApi(el, {
      core: { fontDirectory: '/static/vendor/alphatab/font/' },
      // scrollMode off 필수 — 내장 커서 자동 스크롤(scrollToY→html.scrollTo smooth)이 재조판마다
      // 페이지를 끌고 내려가던 범인(계측 스택으로 확정 2026-07-09). 커서·스크롤은 전부 자체 관리.
      player: { enablePlayer: false, enableCursor: false, scrollMode: 'off' },
      // 셈여림(f) 표시 끔 — 검출하지 않는 기본값이 붙어 나올뿐더러, 있는 시스템만 효과 밴드가 생겨
      // 오선-타브 간격이 47px/15px 로 들쭉날쭉해짐(실측 — 사용자 지적 "빈 부분 간격 좁음")
      notation: { elements: { effectDynamics: false } },
      display: {
        // 시스템 사이 여백 — 타브 아래 카운트 행 자리(seed 실측: 이 값만 시스템 간격을 실제로 벌림)
        notationStaffPaddingBottom: 26,
        layoutMode: 'Page',
        // 줄당 4마디(12/8 은 마디가 3배 넓어 2마디) — 시안의 systemsLayoutMode 는 이 vendor 미동작(실측)
        barsPerRow: isCompound() ? 2 : 4,
        resources: { // 밝은 종이 카드 위 잉크 색 — 시안 05 지정값
          mainGlyphColor: '#241f16',
          secondaryGlyphColor: 'rgba(36,31,22,0.4)',
          staffLineColor: '#b9ae95',
          barSeparatorColor: '#5c5642',
          scoreInfoColor: '#241f16',
          barNumberColor: '#a9884f',
        },
      },
    });
    // 고정 300ms 대기 폐지 — boundsLookup 준비를 폴링(보통 즉시)해 편집마다 붙던 지연 제거
    api.renderFinished.on(function () { collectWhenReady(0); });
    api.tex(tab.tex);
    window.__at = api; // 진단·배터리용(스코어 모델 접근)
  }

  function collectWhenReady(n) {
    var lookup = api && (api.boundsLookup || (api.renderer && api.renderer.boundsLookup));
    if (!lookup && n < 20) {
      setTimeout(function () { collectWhenReady(n + 1); }, 100);
      return;
    }
    collectBeats();
  }

  function collectBeats() {
    // 시안 v3.2·SP-4 프로토와 동일 API: 스코어 모델 순회 + findBeat
    var lookup = api.boundsLookup || (api.renderer && api.renderer.boundsLookup);
    if (!lookup) return;
    var quarterSec = 60 / tab.bpm;
    beats = [];
    barBounds = {};
    api.score.tracks[0].staves[0].bars.forEach(function (bar) {
      bar.voices.forEach(function (voice) {
        voice.beats.forEach(function (beat) {
          var bb = lookup.findBeat(beat);
          if (!bb) return;
          var r = bb.realBounds;
          var barB = bb.barBounds ? bb.barBounds.realBounds : null;
          // 960tick=4분 기준: 4/4 v1 슬롯(16분)=240tick / 4/4 v2 슬롯(박/12)=80tick / 12/8=120tick
          var TICKS_SLOT = isCompound() ? 120 : (isMixed() ? 80 : 240);
          var TICKS_BAR = isCompound() ? 5760 : 3840;
          var barIdx = Math.floor(beat.absolutePlaybackStart / TICKS_BAR);
          var slotIdx = Math.round(beat.absolutePlaybackStart / TICKS_SLOT);
          beats.push({
            // 동적 그리드(slots)가 있으면 실연주 시각, 없으면 균일 그리드 — 인트로 반칸 밀림 실증
            startSec: (tab.slots && tab.slots[slotIdx] != null)
              ? tab.slots[slotIdx]
              : (tab.offset || 0) + beat.absolutePlaybackStart / 960 * quarterSec,
            isRest: beat.isRest,
            slot: slotIdx, // 전역 슬롯(오프그리드 0건 실측)
            barIdx: barIdx,
            // onNotesBounds = 노트헤드 영역 — realBounds 는 부점·코드 심볼까지 포함해 중심이 밀림(검수 문서 B)
            onX: (bb.onNotesX != null ? bb.onNotesX : r.x + r.w / 2),
            x: r.x, y: r.y, w: r.w, h: r.h,
            barX: barB ? barB.x : r.x, barW: barB ? barB.w : r.w,
            barY: barB ? barB.y : r.y, barH: barB ? barB.h : r.h,
          });
          if (barB && !barBounds[barIdx]) {
            barBounds[barIdx] = { x: barB.x, w: barB.w, y: barB.y, h: barB.h };
          }
        });
      });
    });
    // 줄(system)별 타브 보표 하단 y — 카운트 행은 타브 '아래'(위 배치는 G현 숫자와 겹침 실증,
    // 오선-타브 사이 간격은 vendor 설정으로 못 늘림 — 3노브 실측 무효)
    tabBotByRowY = {};
    tabMidByRowY = {};
    (lookup.staffSystems || []).forEach(function (sys) {
      var mb = sys.bars && sys.bars[0];
      if (!mb || !mb.bars || mb.bars.length < 2) return;
      var tb = mb.bars[mb.bars.length - 1].realBounds;
      tabBotByRowY[Math.round(mb.bars[0].realBounds.y)] = tb.y + tb.h;
      tabMidByRowY[Math.round(mb.bars[0].realBounds.y)] = tb.y + tb.h / 2;
    });
    beats.sort(function (a, b) { return a.startSec - b.startSec; });
    placeLowConfDots();
    placeCountLabels();
    placeLyrics();
    if (pendingPopGi != null && editMode) { // 조판 고스트 점 추가 → 새 음표 옆에 팝오버
      var pgi = pendingPopGi;
      pendingPopGi = null;
      var pIdx = tab.notes.findIndex(function (n) { return n.gi === pgi; });
      var pb = null;
      noteBeats().forEach(function (b6) { if (pb == null && b6.slot === pgi) pb = b6; });
      if (pIdx >= 0 && pb) openPopover(pIdx, pb);
    }
    updateCursor(); // 정지 상태에서도 현재 위치를 악보에 표시 (믹서처럼 — 사용자 피드백)
    if (pendingScrollY != null) { // 편집 재조판 후 보던 위치 복원(높이 고정의 이중 안전망)
      window.scrollTo(0, pendingScrollY);
      pendingScrollY = null;
    }
    unfreezeScore(); // 새 조판 준비 완료 — 옛 화면 덮개 제거(그 전까지 화면 정지)
    // 높이 고정은 덮개 제거 뒤에 풀기 — 먼저 풀면 페이지 높이가 줄며 스크롤이 밀림(잔여 꿀렁임)
    document.getElementById('alphaTab').style.minHeight = '';
    window.__tabReady = true;
  }

  /* ---- 박자 세기 라벨 (1 e n a — 사용자 확정 표기) ----
     논리 행은 정량화 데이터(gi/glen)에서 파생(렌더 추측 제거), 마디당 16칸 전부 표시,
     어택 없는 칸(지속·쉼)은 괄호. 좌표: 어택=onNotesBounds, 그 외=인접 렌더 비트 보간
     (마디 폭 등분 매핑은 기각된 방법 — docs/rhythm-counts-review-2026-07-07.md). */
  var COUNT_SYL = ['1', 'e', 'n', 'a', '2', 'e', 'n', 'a', '3', 'e', 'n', 'a', '4', 'e', 'n', 'a'];
  // 배치 이력: 음표 밑(겹침)→줄 맨 위(마디번호·코드 충돌)→타브 위 15px(G현 숫자와 겹침 — 5차 실증)
  // → 타브 '아래'. E현 숫자가 하단선 밖으로 ~6px 나오므로 +9, 아랫줄과의 여백은 조판 설정으로 확보.
  var COUNT_BELOW_TAB = 9;

  function placeCountLabels() {
    var overlay = document.getElementById('at-overlay');
    overlay.querySelectorAll('.count-label, .add-dot').forEach(function (d) { d.remove(); });
    if (!beats.length || !tab || !tab.notes) return;

    var attack = {};   // 전역 16분 슬롯 -> true (음이 시작)
    var sustain = {};  // 앞 음이 끌리는 중
    tab.notes.forEach(function (nt) {
      attack[nt.gi] = true;
      for (var k = 1; k < nt.glen; k++) sustain[nt.gi + k] = true;
    });

    var anchors = {};  // slot -> {x, rowY}
    beats.forEach(function (b) {
      if (anchors[b.slot] == null) anchors[b.slot] = { x: b.onX, rowY: b.barY };
    });
    var anchorSlots = Object.keys(anchors).map(Number).sort(function (a, b2) { return a - b2; });

    function xForSlot(g, bb) {
      if (anchors[g]) return anchors[g].x;
      var prev = null, next = null;
      for (var i2 = 0; i2 < anchorSlots.length; i2++) {
        if (anchorSlots[i2] <= g) prev = anchorSlots[i2];
        else { next = anchorSlots[i2]; break; }
      }
      if (prev == null) return null;
      var pa = anchors[prev];
      var nx, nslot;
      if (next != null && anchors[next].rowY === pa.rowY) {
        nx = anchors[next].x; nslot = next;
      } else {
        nx = bb.x + bb.w; nslot = null; // 줄 끝 — 마디 오른끝을 다음 앵커로
      }
      var span = (nslot == null ? Math.ceil((g + 1) / barSlots()) * barSlots() : nslot) - prev;
      if (span <= 0) return pa.x;
      return pa.x + (nx - pa.x) * (g - prev) / span;
    }

    // 적응형 세분(사용자 4차 확정): 기본 = 1 n 2 n 3 n 4 n (8칸),
    // 16분 어택(e·a 슬롯)이 실제로 있는 박만 1 e n a 로 쪼갬. 안 쓰는 칸도 회색으로 표시.
    // 밀도가 절반이라 모든 어택 라벨을 음표·타브와 같은 세로선(onNotesBounds)에 앵커 가능 — 정렬 복원.
    Object.keys(barBounds).forEach(function (biStr) {
      var bi = parseInt(biStr, 10);
      var bb = barBounds[bi];
      var tabBot = tabBotByRowY[Math.round(bb.y)];
      var y = (tabBot != null ? tabBot : bb.y + bb.h + 106) + COUNT_BELOW_TAB; // 타브 보표 바로 아래

      // 표시할 슬롯 — 4/4 v1: 박마다 [숫자, n](e/a 어택 있으면 확장) · v2: 혼합(e·a/la·li)
      // · 12/8: 펄스 12개(1 la li)
      var slots = [];
      if (isCompound()) {
        for (var p = 0; p < 12; p++) slots.push(bi * 48 + p * 4);
      } else if (isMixed()) {
        for (var q2 = 0; q2 < 4; q2++) {
          var b2 = bi * 48 + q2 * 12;
          var has16 = attack[b2 + 3] || attack[b2 + 9];
          var hasTu = attack[b2 + 4] || attack[b2 + 8];
          slots.push(b2);
          if (has16) { slots.push(b2 + 3); slots.push(b2 + 9); }
          // 편집 모드: 16분이 없는 박엔 셋잇단 자리(la·li)도 입력 후보로 연다(사용자 요청 2026-07-09)
          if (hasTu || (editMode && !has16)) { slots.push(b2 + 4); slots.push(b2 + 8); }
          if (!hasTu) slots.push(b2 + 6);
          slots.sort(function (a, b3) { return a - b3; });
        }
      } else {
        for (var q = 0; q < 4; q++) {
          var base = bi * 16 + q * 4;
          var subdivided = attack[base + 1] || attack[base + 3];
          slots.push(base);
          if (subdivided) slots.push(base + 1);
          slots.push(base + 2);
          if (subdivided) slots.push(base + 3);
        }
      }
      // 좌표: 어택/렌더 비트가 있는 슬롯 = 그 비트 onNotesX(음표·타브와 일렬).
      // 나머지는 이웃 확정 좌표 사이 슬롯 비례 보간, 마지막 경계는 마디 오른끝.
      function interpXs(sl) {
        var xs2 = sl.map(function (g) { return anchors[g] ? anchors[g].x : null; });
        for (var i5 = 0; i5 < xs2.length; i5++) {
          if (xs2[i5] != null) continue;
          var p = i5 - 1;
          while (p >= 0 && xs2[p] == null) p--;
          var nx = i5 + 1;
          while (nx < xs2.length && xs2[nx] == null) nx++;
          var x0 = p >= 0 ? xs2[p] : bb.x + 8;
          var s0 = p >= 0 ? sl[p] : bi * barSlots();
          var x1 = nx < xs2.length ? xs2[nx] : bb.x + bb.w - 6;
          var s1 = nx < xs2.length ? sl[nx] : (bi + 1) * barSlots();
          xs2[i5] = s1 === s0 ? x0 : x0 + (x1 - x0) * (sl[i5] - s0) / (s1 - s0);
        }
        return xs2;
      }
      var xs = interpXs(slots);
      var tabMid = tabMidByRowY[Math.round(bb.y)];
      slots.forEach(function (g, idx) {
        var s = g - bi * barSlots();
        var isAttack = !!attack[g];
        var onbeat = isCompound() ? (s % 12 === 0) : (s % 4 === 0);
        var div = document.createElement('div');
        div.className = 'count-label' + (onbeat ? ' count-onbeat' : '') + (isAttack ? ' count-attack' : ' count-quiet');
        div.textContent = sylFor(s);
        div.dataset.g = g;
        div.dataset.kind = isAttack ? 'attack' : (sustain[g] ? 'sustain' : 'rest');
        div.style.left = Math.min(xs[idx], bb.x + bb.w - 4) + 'px';
        div.style.top = y + 'px';
        overlay.appendChild(div);
        // 편집 모드: 셋잇단 자리(la·li)만 악보(타브 보표) 안에 고스트(+) — 16분 자리는
        // 아래 상단 점 행 블록에서 전부 제공(사용자 지시 2026-07-09: 두 입력을 줄로 분리)
        if (editMode && !isAttack && tabMid != null && isMixed() && (s % 12 === 4 || s % 12 === 8)) {
          var ghost = document.createElement('div');
          ghost.className = 'add-dot add-dot-tu';
          ghost.title = '셋잇단 음표 추가';
          ghost.dataset.g = g;
          ghost.style.left = Math.min(xs[idx], bb.x + bb.w - 4) + 'px';
          ghost.style.top = tabMid + 'px';
          ghost.addEventListener('click', function (e) {
            e.stopPropagation();
            addNoteAt(g, 0, true);
          });
          overlay.appendChild(ghost);
        }
      });

      // 편집 모드: 상단 점 행(음표 수정 점 사이)의 빈 16분 자리 전부에 고스트(+) —
      // "e·a 를 쓰는 마디에서만 추가 가능" 제약 해소(사용자 지시 2026-07-09)
      if (editMode) {
        var dotY = null;
        beats.forEach(function (b5) {
          if (b5.barIdx === bi) dotY = dotY == null ? b5.y : Math.min(dotY, b5.y);
        });
        if (dotY != null) {
          var gslots = [];
          if (isCompound()) {
            for (var pc = 0; pc < 12; pc++) gslots.push(bi * 48 + pc * 4);
          } else if (isMixed()) {
            for (var qb = 0; qb < 4; qb++) {
              [0, 3, 6, 9].forEach(function (o) { gslots.push(bi * 48 + qb * 12 + o); });
            }
          } else {
            for (var s6 = 0; s6 < 16; s6++) gslots.push(bi * 16 + s6);
          }
          var gxs = interpXs(gslots);
          gslots.forEach(function (g6, i6) {
            if (attack[g6]) return; // 음표 있는 자리엔 수정 점이 이미 있음
            var gh = document.createElement('div');
            gh.className = 'add-dot';
            gh.title = '여기에 음표 추가';
            gh.dataset.g = g6;
            gh.style.left = Math.min(gxs[i6], bb.x + bb.w - 4) + 'px';
            gh.style.top = dotY + 'px';
            gh.addEventListener('click', function (e) {
              e.stopPropagation();
              addNoteAt(g6, 0, true);
            });
            overlay.appendChild(gh);
          });
        }
      }
    });
  }

  document.getElementById('count-check').addEventListener('change', function (e) {
    document.getElementById('at-overlay').classList.toggle('hide-counts', !e.target.checked);
    saveShared({ countOn: e.target.checked }); // 토글 상태 저장·복원 (REQ-TAB-007 후보 인수 조건)
  });
  document.getElementById('lyrics-check').addEventListener('change', function (e) {
    document.getElementById('at-overlay').classList.toggle('hide-lyrics', !e.target.checked);
    saveShared({ lyricsOn: e.target.checked });
  });
  document.getElementById('attack-check').addEventListener('change', function (e) {
    document.getElementById('flow-inner').classList.toggle('hide-attacks', !e.target.checked);
    saveShared({ attackOn: e.target.checked }); // 어택 세로선 표시 토글 저장·복원
  });

  /* ---- 가사(받아쓰기 초안) — 단어를 '마디 안 실제 시각 위치'에(사용자 지시 2026-07-11:
     마디 초반에 몰리면 안 됨). x 는 재생 커서와 같은 비트 앵커 보간. ---- */
  function lyricXForTime(t, bi) {
    var bb = barBounds[bi];
    if (!bb) return null;
    var i = -1;
    for (var k = 0; k < beats.length; k++) {
      if (beats[k].startSec <= t) i = k; else break;
    }
    var x;
    if (i < 0) {
      x = bb.x + 4;
    } else {
      var cur = beats[i];
      var next = beats[i + 1];
      if (next && next.startSec > cur.startSec) {
        var frac = Math.min(1, (t - cur.startSec) / (next.startSec - cur.startSec));
        var curBB = barBounds[cur.barIdx];
        var endX = (next.barY === cur.barY) ? next.onX
          : (curBB ? curBB.x + curBB.w : cur.onX);
        x = cur.onX + (endX - cur.onX) * frac;
      } else {
        x = cur.onX;
      }
    }
    return Math.max(bb.x + 2, Math.min(x, bb.x + bb.w - 6));
  }

  function placeLyricWord(word, t, manual, overlay) {
    var bi = Math.max(0, Math.floor(timeSlot(t) / barSlots()));
    var bb = barBounds[bi];
    if (!bb) return;
    var x = lyricXForTime(t, bi);
    var tabBot = tabBotByRowY[Math.round(bb.y)];
    var y = (tabBot != null ? tabBot : bb.y + bb.h + 106) + COUNT_BELOW_TAB + 13;
    var div = document.createElement('div');
    div.className = 'lyric-label' + (manual ? ' manual' : '');
    div.textContent = word;
    div.style.left = x + 'px';
    div.style.top = y + 'px';
    overlay.appendChild(div);
  }

  function placeLyrics() {
    var overlay = document.getElementById('at-overlay');
    overlay.querySelectorAll('.lyric-label').forEach(function (d) { d.remove(); });
    var ly = tab && tab.lyrics;
    if (!ly || ly.status !== 'ready' || !ly.segments || !ly.segments.length) return;
    ly.segments.forEach(function (seg) {
      if (seg.words && seg.words.length && !seg.manual) {
        seg.words.forEach(function (w) {
          placeLyricWord(w.w, w.s + 0.01, false, overlay);
        });
        return;
      }
      // 폴백(단어 시각 없음 — 구버전·직접 고친 소절): 소절 구간 안에 시간 균등 배치
      var words = String(seg.text || '').split(/\s+/).filter(Boolean);
      var span = Math.max(0.2, seg.e - seg.s);
      words.forEach(function (word, k) {
        var t = seg.s + span * (k + 0.15) / words.length;
        placeLyricWord(word, t, !!seg.manual, overlay);
      });
    });
  }

  /* ---- 낮은 확신 점 + 보정 팝오버 (REQ-TAB-003) + 전체 편집 모드 (2026-07-08) ---- */
  var CONF_TH = 0.75;
  var editing = null; // {noteIdx, dotEl}
  var editMode = false; // 켜면 모든 음표 수정·삭제 + 흐름 뷰 빈 칸 클릭으로 추가
  var lastEditGi = null; // 가장 최근에 추가·수정한 음 — 빨간 점으로 표시(사용자 요청 2026-07-09)
  var lastEditLabel = ''; // 무엇을 했는지 — 점·테두리 툴팁으로 노출(사용자 요청: 어디가 수정됐는지)
  function markRecent(g, label) {
    lastEditGi = g;
    lastEditLabel = label || '';
  }

  function noteBeats() {
    return beats.filter(function (b) { return !b.isRest; });
  }

  function placeLowConfDots() {
    var overlay = document.getElementById('at-overlay');
    overlay.querySelectorAll('.lowconf-dot, .lowconf-ring, .recent-bar, .recent-bar-tag').forEach(function (d) { d.remove(); });
    // 마지막으로 수정한 마디 전체를 빨간 테두리로 — 재조판 후 어디가 바뀌었는지 즉시 식별(사용자 요청)
    if (editMode && lastEditGi != null && tab && tab.notes) {
      var rbi = Math.floor(lastEditGi / barSlots());
      var rbb = barBounds[rbi];
      if (rbb) {
        var rTop = null;
        beats.forEach(function (b3) {
          if (b3.barIdx === rbi) rTop = rTop == null ? b3.y : Math.min(rTop, b3.y);
        });
        if (rTop == null) rTop = rbb.y;
        var rBot = tabBotByRowY[Math.round(rbb.y)];
        var box = document.createElement('div');
        box.className = 'recent-bar';
        box.title = '마지막 수정 마디' + (lastEditLabel ? ' — ' + lastEditLabel : '');
        // 위 여유 36px: 수정 점 행(점+그림자)과 마디 위 코드 심볼까지 포함(테두리가 점에 걸치던 것 교정)
        box.style.left = (rbb.x - 4) + 'px';
        box.style.top = (rTop - 36) + 'px';
        box.style.width = (rbb.w + 8) + 'px';
        box.style.height = (((rBot != null ? rBot + 26 : rbb.y + rbb.h)) - (rTop - 36)) + 'px';
        overlay.appendChild(box);
        if (lastEditLabel) { // 무엇을 했는지 보이는 라벨 — 테두리는 pointer-events:none 이라 툴팁 불가
          var tag = document.createElement('div');
          tag.className = 'recent-bar-tag';
          tag.textContent = lastEditLabel;
          tag.style.left = (rbb.x - 4) + 'px';
          tag.style.top = (rTop - 36 - 18) + 'px';
          overlay.appendChild(tag);
        }
      }
    }
    // 슬롯(gi) 기준 매핑 — 순번 매핑은 긴 음이 박 경계에서 붙임줄로 갈라지면
    // 그 뒤 점 전부가 한 칸씩 밀림(사용자 실증 2026-07-09: 점이 엉뚱한 음표 위에)
    var bySlot = {};
    noteBeats().forEach(function (b2) { if (bySlot[b2.slot] == null) bySlot[b2.slot] = b2; });
    tab.notes.forEach(function (nt, i) {
      var low = nt.conf < CONF_TH;
      if (!low && !editMode) return;
      var b = bySlot[nt.gi];
      if (!b) return;
      var dot = document.createElement('div');
      // 초록 = 직접 추가·수정·확인한 음(conf 1.0) · 빨강 = 그중 가장 최근 것 — 자동 초안과 구분
      var recent = nt.gi === lastEditGi;
      dot.className = low ? 'lowconf-dot'
        : (nt.conf >= 1.0 ? 'lowconf-dot edit-dot human-dot' : 'lowconf-dot edit-dot');
      if (recent) dot.className += ' recent-dot';
      // 무엇을 고쳤는지 툴팁에 — nt.edit 는 편집 시 기록되어 저장까지 따라감(사용자 요청 2026-07-09)
      dot.title = recent ? ('방금: ' + (lastEditLabel || nt.edit || '수정'))
        : (low ? '확신 낮음 — 클릭해서 보정'
          : (nt.conf >= 1.0
            ? '직접 고침' + (nt.edit ? '(' + nt.edit + ')' : '') + ' — 클릭해서 고치기'
            : '클릭해서 고치기'));
      dot.style.left = b.onX + 'px'; // 노트헤드 앵커(검수 문서 B 교정)
      dot.style.top = b.y + 'px';
      dot.dataset.note = i;
      dot.addEventListener('click', function (e) {
        e.stopPropagation();
        openPopover(i, b);
      });
      overlay.appendChild(dot);
    });
  }

  var popover = document.getElementById('correction-popover');

  // 다른 자리 후보 — 운지 선택은 연주자 몫. ①같은 음 다른 줄·프렛 ②옥타브 치환(도착 자리 표시,
  // 음이 옥타브만큼 달라지므로 자동 배치는 안 하고 선택지로만 — 정직 원칙)
  function fillAlts(noteIdx) {
    var box = document.getElementById('corr-alts');
    box.innerHTML = '';
    var nt = tab.notes[noteIdx];
    // 앞 음이 없으면 '삭제→앞 음 늘림' 불가 — 눌러도 아무 일 없는 죽은 버튼 방지
    var ext = document.getElementById('corr-delete-extend');
    if (ext) ext.disabled = !tab.notes.some(function (o) { return o.gi < nt.gi; });
    // 이 박에 셋잇단 음이 있으면 '16분으로 되돌리기' 노출 — 셋잇단 추가(박 변환)의 반대 방향
    var bs = document.getElementById('corr-beat-straight');
    if (bs) {
      var b0 = Math.floor(nt.gi / 12) * 12;
      bs.hidden = !(isMixed() && tab.notes.some(function (o) {
        return o.gi >= b0 && o.gi < b0 + 12 && (o.gi % 12 === 4 || o.gi % 12 === 8);
      }));
    }
    var open = [28, 33, 38, 43];
    function addBtn(label, midi, s, fret) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-outline btn-sm';
      b.textContent = label;
      b.addEventListener('click', function () {
        pushUndo();
        editTargets(nt).forEach(function (n) {
          n.midi = midi; n.string = s; n.fret = fret; n.conf = 1.0;
          n.edit = '자리 변경: ' + label;
        });
        markRecent(nt.gi, '자리 변경: ' + label);
        closePopover(); saveNotes();
      });
      box.appendChild(b);
    }
    open.forEach(function (o, s) {
      var fret = nt.midi - o;
      if (fret < 0 || fret > 15 || s === nt.string) return;
      addBtn('같은 음: ' + (4 - s) + '번줄 ' + fret + '프렛', nt.midi, s, fret);
    });
    [-12, 12].forEach(function (d) {
      var m = nt.midi + d;
      if (m < 28 || m > 58) return;
      // 현재 프렛과 가장 가까운 자리로 미리 계산해 보여줌
      var best = null;
      open.forEach(function (o, s) {
        var fret = m - o;
        if (fret < 0 || fret > 15) return;
        var score = Math.abs(fret - nt.fret) + Math.abs(s - nt.string) * 0.5;
        if (!best || score < best.score) best = { s: s, fret: fret, score: score };
      });
      if (best) {
        addBtn('옥타브' + (d > 0 ? '↑' : '↓') + ': ' + (4 - best.s) + '번줄 ' + best.fret + '프렛',
          m, best.s, best.fret);
      }
    });
    box.hidden = !box.children.length;
  }

  function openPopover(noteIdx, beatBounds) {
    document.getElementById('at-overlay').appendChild(popover); // 흐름 뷰에 가 있었을 수 있음
    editing = { noteIdx: noteIdx };
    fillAlts(noteIdx);
    popover.hidden = false;
    // 팝오버는 그 줄(오선+타브+카운트) 전체 아래 — 위에 띄우면 타브 숫자를 가림(사용자 실증 2026-07-09)
    var tabBot = tabBotByRowY[Math.round(beatBounds.barY != null ? beatBounds.barY : beatBounds.y)];
    popover.style.left = Math.max(0, (beatBounds.onX != null ? beatBounds.onX : beatBounds.x) - 24) + 'px';
    popover.style.top = ((tabBot != null ? tabBot : beatBounds.y + beatBounds.h) + 26) + 'px';
    // 링은 항상 1개 — 점을 연달아 클릭하면 이전 링이 남아 2개 이상 쌓이던 결함(사용자 실증 2026-07-09)
    document.querySelectorAll('.lowconf-ring').forEach(function (r) { r.remove(); });
    var ring = document.createElement('div');
    ring.className = 'lowconf-ring';
    // 링은 노트헤드 세로선(onX) — realBounds 중심은 부점·셋잇단 괄호까지 포함해 음표 옆으로 샘(사용자 실증)
    ring.style.left = (beatBounds.onX != null ? beatBounds.onX : beatBounds.x + beatBounds.w / 2) + 'px';
    ring.style.top = (beatBounds.y + beatBounds.h / 2) + 'px';
    document.getElementById('at-overlay').appendChild(ring);
  }

  function closePopover() {
    popover.hidden = true;
    document.querySelectorAll('.lowconf-ring').forEach(function (r) { r.remove(); });
    editing = null;
  }

  // 빈 자리에 음표 추가(흐름 뷰 클릭·조판 고스트 점 공용) — 개방현으로 넣고 팝오버로 조정.
  // fromScore: 조판에서 추가하면 재조판 후 그 음표 옆에 조판 팝오버 —
  // 흐름 팝오버를 열면 화면 위쪽 엉뚱한 곳(흐름 스트립)에 떠서 못 찾음(사용자 실증 2026-07-09)
  var pendingPopGi = null;
  function addNoteAt(gi, string, fromScore) {
    if (!tab || gi < 0) return;
    if (tab.notes.some(function (n) { return n.gi === gi; })) return;
    var OPEN = [28, 33, 38, 43];
    // 기본 길이 = 그 자리 가족의 한 단위(16분=3칸·셋잇단=4칸·구그리드=1칸) — 1칸 고정은
    // 조판 폴백·'앞 음 늘림' 계산에 찌꺼기 길이를 남김
    var glen0 = isMixed() ? ((gi % 12 === 4 || gi % 12 === 8) ? 4 : 3) : 1;
    var nt = {
      start: Math.round(slotTime(gi) * 1000) / 1000,
      dur: Math.round((slotTime(gi + glen0) - slotTime(gi)) * 1000) / 1000,
      midi: OPEN[string], conf: 1.0, gi: gi, glen: glen0, string: string, fret: 0,
    };
    pushUndo();
    if (isMixed() && (gi % 12 === 4 || gi % 12 === 8)) {
      // 셋잇단 자리에 추가 = 이 박을 셋잇단으로 — 같은 박의 16분·8분 음을 셋잇단 자리로 옮긴다.
      // 안 옮기면 서버 가족 투표가 S 로 갈려 추가한 음이 e(3) 위치로 튕김(사용자 실증 2026-07-09)
      var beatBase = Math.floor(gi / 12) * 12;
      var remap = { 3: 4, 6: 8, 9: 8 }; // 음악 관례: 가운데 8분(n)→셋째 셋잇단(li)
      tab.notes.forEach(function (n) {
        if (n.gi < beatBase || n.gi >= beatBase + 12) return;
        var o = n.gi % 12;
        if (remap[o] == null) return;
        var t = beatBase + remap[o];
        var taken = function (x) {
          return x === gi || tab.notes.some(function (m) { return m !== n && m.gi === x; });
        };
        if (taken(t)) t = beatBase + (remap[o] === 8 ? 4 : 8); // 충돌 시 남은 셋잇단 자리
        if (taken(t)) return; // 둘 다 차면 그대로 — 서버 위생이 정리
        n.gi = t;
        n.start = Math.round(slotTime(t) * 1000) / 1000;
        n.conf = 1.0;
        n.edit = '셋잇단 자리로 이동(박 변환)';
      });
    }
    var at = tab.notes.findIndex(function (n) { return n.gi > gi; });
    if (at < 0) at = tab.notes.length;
    nt.edit = (gi % 12 === 4 || gi % 12 === 8) && isMixed() ? '셋잇단 음표 추가' : '음표 추가';
    tab.notes.splice(at, 0, nt);
    markRecent(gi, nt.edit);
    if (fromScore) pendingPopGi = gi;
    saveNotes().then(function () {
      if (fromScore) return; // 조판 팝오버는 재조판 완료 시점(postRender)에 연다
      var idx = tab.notes.findIndex(function (n) { return n.gi === gi; });
      if (idx >= 0) openPopoverFlow(idx);
    });
  }

  // 흐름 뷰용 팝오버 — 스트립 안(overflow 안 잘리게 top 은 스트립 내부)에 띄움
  function openPopoverFlow(noteIdx) {
    // 스트립(overflow 클립) 밖의 wrap 에 붙임 — 자리 대안 행이 늘어 세로로 잘리던 문제 방지
    var wrap = document.querySelector('.flow-wrap');
    wrap.appendChild(popover);
    editing = { noteIdx: noteIdx };
    fillAlts(noteIdx);
    popover.hidden = false;
    var sc = document.getElementById('flow-scroll');
    var nt = tab.notes[noteIdx];
    var x = flowX(nt.gi) - sc.scrollLeft + sc.offsetLeft;
    popover.style.left = Math.max(4, Math.min(x - 24, wrap.clientWidth - 270)) + 'px';
    popover.style.top = '4px';
  }

  // 편집 모드 토글 — 조판엔 전 음표 점, 흐름 뷰는 클릭=수정/추가로 전환
  document.getElementById('edit-check').addEventListener('change', function (e) {
    editMode = e.target.checked;
    document.getElementById('flow-inner').classList.toggle('edit-mode', editMode);
    document.querySelector('.flow-caption').textContent = editMode
      ? '음표 고치기 — 숫자(음표)를 누르면 고치기 · 줄 위 빈 곳을 누르면 그 자리에 추가 · 아래 악보에선 점 행의 빈 동그라미=음표 추가, 초록 점선=셋잇단 추가'
      : '흐름 타브 — 박자 간격이 일정해요 · 클릭하면 그 위치로 이동 · 아래 악보는 인쇄용 표기';
    closePopover();
    placeLowConfDots();
    placeCountLabels(); // 편집 모드 고스트(+) 점 갱신
  });

  // 되돌리기 — 편집 사고(이동·삭제 실수)의 안전망(사용자 요청 2026-07-09)
  var undoStack = [];
  function updateUndoBtn() { // 몇 단계 쌓였는지 노출 + 없으면 비활성(사용자 요청 2026-07-09)
    var btn = document.getElementById('btn-undo');
    if (!btn) return;
    btn.disabled = !undoStack.length;
    btn.textContent = undoStack.length ? '되돌리기 (' + undoStack.length + ')' : '되돌리기';
  }
  function pushUndo() {
    // 조판 원문(tex)·코드까지 함께 스냅샷 — 되돌릴 때 서버 재계산을 기다릴 필요 없이 즉시 복원
    undoStack.push(JSON.stringify({
      notes: tab.notes, tex: tab.tex, chords: tab.chords || null, key: tab.key_json || null,
    }));
    if (undoStack.length > 30) undoStack.shift();
    updateUndoBtn();
  }
  function doUndo() {
    if (!undoStack.length || !tab) return;
    var snap = JSON.parse(undoStack.pop());
    tab.notes = snap.notes;
    tab.tex = snap.tex;
    if (snap.chords) tab.chords = snap.chords;
    if (snap.key) tab.key_json = snap.key;
    updateUndoBtn();
    renderFlow();     // 화면은 즉시 이전 상태로 —
    freezeScore();
    renderScore();    // 이미 알고 있는 이전 조판 원문으로 바로 재조판
    quietSave = true; // 백그라운드 저장이 같은 화면을 또 그리지 않게(중복 갱신 = 멈춤 체감)
    saveNotes();      // 영속화는 백그라운드 — 응답 tex 가 같으면(정상) 추가 재조판 없음
  }

  var pendingScrollY = null; // 재조판 후 스크롤 복원(편집마다 맨 위로 튐 — 사용자 실증)
  var quietSave = false; // 화면은 이미 갱신됨(되돌리기 등) — 저장 응답에서 중복 갱신 생략
  var noteChain = Promise.resolve(); // 연속 편집 PUT 경쟁 방지(늦은 응답이 최신 notes 를 되돌리는 유실 — 리뷰 확정 결함)
  function saveNotes() {
    noteChain = noteChain.then(saveNotesNow, saveNotesNow);
    return noteChain;
  }
  function saveNotesNow() {
    var sy = window.scrollY;
    var sx = document.getElementById('flow-scroll').scrollLeft;
    return fetch('/api/songs/' + songId + '/tab/notes', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes: tab.notes }),
    }).then(function (r) {
      if (!r.ok) { // 예: 재분석 중 409 — 침묵 파손 대신 안내 후 최신 상태 재로드(리뷰 확정 결함)
        alert('지금은 저장할 수 없어요 — 분석이 끝난 뒤 다시 시도해주세요');
        refreshTab();
        return null;
      }
      return r.json();
    }).then(function (res) {
      if (!res) return;
      var texChanged = res.tex !== tab.tex;
      var quiet = quietSave;
      quietSave = false;
      tab.tex = res.tex;
      if (res.notes) tab.notes = res.notes; // 서버 정규화(지속 겹침 자름) 반영
      if (res.chords) tab.chords = res.chords; // 코드 행·스트립 신선도(재계산 결과 채택)
      if (res.key) tab.key_json = res.key;
      if (quiet && !texChanged) return; // 화면은 이미 최신(되돌리기 로컬 복원) — 중복 갱신 생략
      renderFlow();  // 보정 반영 — 흐름 뷰
      document.getElementById('flow-scroll').scrollLeft = sx;
      if (texChanged) {
        pendingScrollY = sy;
        freezeScore(); // 재조판 완료까지 옛 조판을 그대로 표시(꿀렁임 차단)
        renderScore(); // 보정 반영 재조판 — 화면이 잠깐 다시 그려지는 유일한 지점
      } else {
        // 조판 불변(확인=승인 등) — 무거운 전체 재조판 생략, 점만 갱신(느림·새로고침 체감의 주범)
        placeLowConfDots();
        placeCountLabels();
      }
    });
  }

  // '같은 음 전부' 체크 시 — 같은 자리(음·줄·프렛)의 모든 음표에 함께 적용(사용자 요청 2026-07-09)
  function editTargets(nt) {
    var all = document.getElementById('corr-all');
    if (all && all.checked) {
      return tab.notes.filter(function (o) {
        return o.midi === nt.midi && o.string === nt.string && o.fret === nt.fret;
      });
    }
    return [nt];
  }

  document.getElementById('corr-up').addEventListener('click', function () {
    if (!editing) return;
    pushUndo();
    markRecent(tab.notes[editing.noteIdx].gi, '음높이 반음 올림');
    editTargets(tab.notes[editing.noteIdx]).forEach(function (n) {
      n.midi += 1; n.conf = 1.0; n.edit = '음높이 반음 올림'; reassignFret(n);
    });
    closePopover(); saveNotes();
  });
  document.getElementById('corr-down').addEventListener('click', function () {
    if (!editing) return;
    pushUndo();
    markRecent(tab.notes[editing.noteIdx].gi, '음높이 반음 내림');
    editTargets(tab.notes[editing.noteIdx]).forEach(function (n) {
      n.midi -= 1; n.conf = 1.0; n.edit = '음높이 반음 내림'; reassignFret(n);
    });
    closePopover(); saveNotes();
  });
  function moveNote(d) {
    if (!editing) return;
    var nt = tab.notes[editing.noteIdx];
    var target = nt.gi + d;
    if (isMixed()) { // 다음/이전 허용 위치(16분·셋잇단)로 — 48분 단위 이동은 무의미
      var beat = Math.floor(nt.gi / 12);
      var all = [];
      for (var b = Math.max(0, beat - 1); b <= beat + 1; b++) {
        ALLOWED_48.forEach(function (o) { all.push(b * 12 + o); });
      }
      all.sort(function (a, b2) { return a - b2; });
      var i = all.indexOf(nt.gi);
      if (i < 0) i = all.findIndex(function (g) { return g > nt.gi; }) - (d > 0 ? 1 : 0);
      target = all[Math.max(0, Math.min(all.length - 1, i + d))];
    }
    if (target < 0) return;
    if (tab.notes.some(function (o) { return o !== nt && o.gi === target; })) {
      alert('그 칸에는 이미 음표가 있어요 — 먼저 그 음표를 지우거나 옮겨주세요');
      return;
    }
    pushUndo();
    nt.gi = target;
    nt.start = Math.round(slotTime(target) * 1000) / 1000;
    nt.conf = 1.0;
    nt.edit = d > 0 ? '한 칸 뒤로 이동' : '한 칸 앞으로 이동';
    markRecent(target, nt.edit);
    tab.notes.sort(function (a, b) { return a.gi - b.gi; });
    closePopover(); saveNotes();
  }
  document.getElementById('corr-left').addEventListener('click', function () { moveNote(-1); });
  document.getElementById('corr-right').addEventListener('click', function () { moveNote(1); });

  document.getElementById('corr-delete').addEventListener('click', function () {
    if (!editing) return;
    pushUndo();
    // 삭제도 '최근 수정'으로 마킹 — 안 하면 이전 편집의 빨간 점이 엉뚱한 음에 남음(사용자 실증).
    // 지운 자리엔 음이 없어 점은 안 그려지고, 마디 테두리만 위치를 알려준다.
    markRecent(tab.notes[editing.noteIdx].gi, '음표 삭제(쉼표)');
    tab.notes.splice(editing.noteIdx, 1);
    closePopover(); saveNotes();
  });
  // 이 박을 16분으로 되돌리기 — 셋잇단 추가(박 변환)의 역방향(4→3, 8→9)
  document.getElementById('corr-beat-straight').addEventListener('click', function () {
    if (!editing) return;
    var nt0 = tab.notes[editing.noteIdx];
    var b0 = Math.floor(nt0.gi / 12) * 12;
    pushUndo();
    var remap = { 4: 3, 8: 9 };
    tab.notes.forEach(function (n) {
      if (n.gi < b0 || n.gi >= b0 + 12) return;
      var o = n.gi % 12;
      if (remap[o] == null) return;
      var t = b0 + remap[o];
      if (tab.notes.some(function (m) { return m !== n && m.gi === t; })) return;
      n.gi = t;
      n.start = Math.round(slotTime(t) * 1000) / 1000;
      n.conf = 1.0;
      n.edit = '16분 자리로 복귀';
    });
    tab.notes.sort(function (a, b) { return a.gi - b.gi; });
    markRecent(nt0.gi, '박자를 16분으로 되돌림'); // 되돌린 박의 기준 음(재배치됐다면 새 위치)
    closePopover(); saveNotes();
  });
  // 삭제하며 앞 음을 그 자리까지 늘림 — "빈 시간=쉼표 표기" 대신 소리를 잇고 싶을 때(사용자 요청 2026-07-09)
  document.getElementById('corr-delete-extend').addEventListener('click', function () {
    if (!editing) return;
    var nt = tab.notes[editing.noteIdx];
    var prev = null;
    tab.notes.forEach(function (o) {
      if (o !== nt && o.gi < nt.gi && (!prev || o.gi > prev.gi)) prev = o;
    });
    pushUndo();
    if (prev) {
      prev.glen = nt.gi + nt.glen - prev.gi; // 지운 음의 끝까지 이어 울림
      prev.conf = 1.0; // 길이를 사람이 정함
      prev.edit = '길이 늘림(다음 음 삭제)';
      markRecent(prev.gi, prev.edit); // 빨간 점이 앞 음에 붙는 건 의도 — 이 음이 실제로 바뀜(길이)
    }
    tab.notes.splice(editing.noteIdx, 1);
    closePopover(); saveNotes();
  });
  document.getElementById('corr-ok').addEventListener('click', function () {
    if (!editing) return;
    pushUndo();
    tab.notes[editing.noteIdx].conf = 1.0; // 확인 = 사람이 승인
    if (!tab.notes[editing.noteIdx].edit) tab.notes[editing.noteIdx].edit = '확인(승인)';
    markRecent(tab.notes[editing.noteIdx].gi, '확인(승인)');
    closePopover(); saveNotes();
  });
  document.getElementById('btn-undo').addEventListener('click', doUndo);
  // 인쇄/PDF (사용자 요청 2026-07-09) — 새 탭의 인쇄 전용 페이지(악보만·여러 장 분할 정상)
  document.getElementById('btn-print').addEventListener('click', function () {
    window.open('/songs/' + songId + '/tab/print', '_blank');
  });

  function reassignFret(nt) {
    // 음역 밖은 옥타브 접기(클램프 금지 — 클램프는 음정 파괴, 실증: 옥타브 일괄 내리기 사고)
    while (nt.midi < 28) nt.midi += 12;
    while (nt.midi > 58) nt.midi -= 12;
    var open = [28, 33, 38, 43];
    for (var s = open.length - 1; s >= 0; s--) {
      var fret = nt.midi - open[s];
      if (fret >= 0 && fret <= 15) { nt.string = s; nt.fret = fret; return; }
    }
  }

  /* ---- 재생 커서 (REQ-TAB-002): 시간→비트 보간 x, 현재 마디 하이라이트 ---- */
  var highlight = document.getElementById('playhead-highlight');
  var line = document.getElementById('playhead-line');

  /* 소리-화면 싱크 보정은 공용 트랜스포트가 소유(전역 설정) — 여기선 값만 읽는다 */
  function updateCursor() {
    var t = Shell.visualTime(); // 표시 시계 단일 소스(진행바·믹서·코드와 동일 — 자체 공식 금지)
    updateFlowCursor(t); // 흐름 뷰 커서 — 항상 등속
    if (!beats.length) return;
    var i = -1;
    for (var k = 0; k < beats.length; k++) {
      if (beats[k].startSec <= t) i = k; else break;
    }
    window.__cursorIdx = i; // 검증 배터리용(줄바꿈과 무관한 전진 지표)
    if (i < 0) { highlight.hidden = line.hidden = true; return; }
    // 비트 앵커 보간(Guitar Pro 방식): 각 음표·쉼표의 '시각'에 정확히 그 글리프 위에 서고,
    // 그 사이는 연속 이동. 조판 간격이 시간 비례가 아니라 마디 내 등속 커서는 어택과
    // 어긋나 보인다(사용자 실증: 커서가 지나간 뒤 소리 남). 줄당 4마디 균일 배치라
    // 속도 출렁임은 완만하다.
    var cur = beats[i];
    var next = beats[i + 1];
    var bb = barBounds[cur.barIdx];
    window.__cursorBar = cur.barIdx;
    if (!bb) { highlight.hidden = line.hidden = true; return; }
    var x;
    if (next && next.startSec > cur.startSec) {
      var frac = Math.min(1, (t - cur.startSec) / (next.startSec - cur.startSec));
      var endX = next.barY === cur.barY ? next.onX : bb.x + bb.w; // 줄바꿈이면 마디 끝까지
      x = cur.onX + (endX - cur.onX) * frac;
    } else {
      x = cur.onX;
    }
    line.hidden = false;
    line.style.left = x + 'px';
    line.style.top = bb.y + 'px';
    line.style.height = bb.h + 'px';
    highlight.hidden = false;
    highlight.style.left = bb.x + 'px';
    highlight.style.width = bb.w + 'px';
    highlight.style.top = bb.y + 'px';
    highlight.style.height = bb.h + 'px';
    // 자동 스크롤: 커서가 화면 밖이면 따라가기
    var paper = document.getElementById('at-paper');
    if (x < paper.scrollLeft + 40 || x > paper.scrollLeft + paper.clientWidth - 60) {
      paper.scrollLeft = Math.max(0, x - 120);
    }
  }

  /* ---- 악보 클릭 = 그 음표 위치로 이동 (믹서의 파형 클릭과 동일 어포던스) ---- */
  document.getElementById('at-paper').addEventListener('click', function (e) {
    if (e.target.closest('.lowconf-dot')) return; // 점은 보정 팝오버 우선
    if (!popover.hidden) { closePopover(); return; } // 바깥 클릭 = 팝오버 닫기
    if (!beats.length) return;
    var overlayRect = document.getElementById('at-overlay').getBoundingClientRect();
    var cx = e.clientX - overlayRect.left;
    var cy = e.clientY - overlayRect.top;
    var best = null;
    beats.forEach(function (b) {
      var rowDist = Math.abs((b.barY + b.barH / 2) - cy);
      var xDist = Math.abs((b.x + b.w / 2) - cx);
      var score = rowDist * 3 + xDist; // 클릭한 줄(시스템)을 우선 매칭
      if (!best || score < best.score) best = { score: score, b: b };
    });
    if (!best) return;
    player.seek(Math.max(0, best.b.startSec));
    document.getElementById('time-now').textContent = fmt(player.currentTime());
    var dur = duration || player.duration();
    if (dur) document.getElementById('seekbar').value = Math.round(player.currentTime() / dur * 1000);
    updateCursor();
  });

  function showBpm() {
    var b = document.getElementById('bpm-badge');
    // 12/8 은 검출 bpm 이 셋잇단 펄스(3배) — 체감 박자(점4분)로 표시
    b.textContent = isCompound()
      ? 'BPM ' + Math.round(tab.bpm / 3) + ' (12/8·추정)'
      : 'BPM ' + Math.round(tab.bpm) + ' (추정)';
    b.hidden = false;
    if (tab.key_json && tab.key_json.label) {
      // 표기: 메이저 기준 + 마이너 병기 — 메이저 곡도 상대 단조 병기(사용자 지시 2026-07-10 재확인)
      var kd = tab.key_json.display || tab.key_json.label;
      var mn = tab.key_json.minor || (tab.key_json.mode === 'minor' ? tab.key_json.label : '');
      if (mn) kd += ' (' + mn + ')';
      var k = document.getElementById('key-badge');
      k.textContent = '키 ' + kd + ' (추정)';
      k.hidden = false;
    }
  }

  /* ---- 뷰 모듈 등록 — 처음 보일 때 분석 상태 머신 시작(스템·설정·저장은 셸 몫) ---- */
  var needsScoreRender = false;

  Shell.registerView('tab', {
    init: function () {
      Shell.stateReady.then(function () {
        // 박자 세기 토글 복원은 스템 로딩을 기다리지 않는다 — __tabReady(조판 완료) 후에도 복원 전이면
        // 그 틈의 클릭이 기본값을 토글해 반대 값을 저장(실증: 배터리 '토글 복원' 간헐 실패의 진짜 원인)
        var countOn = sharedState.countOn !== false;
        document.getElementById('count-check').checked = countOn;
        document.getElementById('at-overlay').classList.toggle('hide-counts', !countOn);
        var lyricsOn = sharedState.lyricsOn !== false;
        document.getElementById('lyrics-check').checked = lyricsOn;
        document.getElementById('at-overlay').classList.toggle('hide-lyrics', !lyricsOn);
        var attackOn = sharedState.attackOn !== false;
        document.getElementById('attack-check').checked = attackOn;
        document.getElementById('flow-inner').classList.toggle('hide-attacks', !attackOn);
        window.__stateRestored = true; // 배터리: 토글 조작 전 이 플래그 대기
      });
      Shell.ready.then(function () {
        duration = player.duration();
        updateCursor(); // 로드 직후에도 현재 위치 표시(악보가 늦게 렌더되면 collectBeats 가 재호출)
      });
      refreshTab();
    },
    activate: function () {
      if (needsScoreRender) { needsScoreRender = false; renderScore(); }
      updateCursor();
    },
  });

  window.__updateCursor = updateCursor; // 배터리용(정지 중 명시 갱신)
  window.__notes = function () { return tab && tab.notes; }; // 배터리·진단용(클라이언트 노트 상태)
  window.__beatX = function (i) { return beats[i] ? beats[i].onX : null; }; // 배터리용(노트헤드 앵커)
  window.__beatT = function (i) { return beats[i] ? beats[i].startSec : null; };
  window.__barBounds = function (bi) { return barBounds[bi] || null; };   // 배터리용
})();
