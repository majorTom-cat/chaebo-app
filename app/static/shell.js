/* 연습 화면 셸 — 한 문서(믹서·타브·코드 악보 3뷰)의 공유 골격.
   플레이어·공유 상태·자동 저장·공용 트랜스포트·분석 메타·뷰 전환(리로드 없음)을 문서당 1개 소유.
   (사용자 실증 2026-07-10: 페이지 3벌 구조는 탭 전환마다 깜빡임+스템 재로드+상단 구조 드리프트)

   뷰 모듈 계약: Shell.registerView(name, { init(), activate?() })
   - init: 그 뷰가 처음 보일 때 1회(초기 뷰면 파일 파스 직후). DOM 배선·데이터 로드.
   - activate: 다시 보일 때마다 — 숨김 동안 밀린 화면 갱신(커서·파형 등).
   훅: Shell.on('tick'|'seek'|'loop'|'sync'|'play'|'meta', cb, viewName?)
   - viewName 을 주면 그 뷰가 활성일 때만 호출(숨은 뷰의 헛일 방지). 'meta' 는 관례상 전 뷰 수신. */
window.Shell = (function () {
  'use strict';

  var songId = document.body.dataset.songId;
  var player = new SyncPlayer();
  var state = { position: 0, rate: 1.0, volumes: {}, muted: {}, solo: null, loopA: null, loopB: null, loops: [], activeLoop: null };
  var active = document.body.dataset.view || 'mixer';
  var views = {};
  var inited = {};
  var hooks = { tick: [], seek: [], loop: [], sync: [], play: [], meta: [] };
  var lastMeta = null;

  function on(ev, cb, viewName) { hooks[ev].push({ view: viewName || null, cb: cb }); }
  function emit(ev, a, b) {
    hooks[ev].forEach(function (h) {
      if (h.view && h.view !== active) return;
      h.cb(a, b);
    });
  }

  /* ---- 자동 저장 (REQ-LIB-003) — 문서당 1벌: 디바운스 + 이탈 keepalive + 재생 중 5초 ---- */
  var saveTimer = null;
  function save(patch) {
    if (patch) Object.assign(state, patch);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { saveNow(); }, 800);
  }
  function saveNow(keepalive) {
    if (!player.audios.length) return; // 스템 로드 전 저장 = 위치 0 으로 덮어씀 방지
    state.position = player.currentTime();
    fetch('/api/songs/' + songId + '/state', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(state),
      keepalive: !!keepalive,
    });
  }
  window.addEventListener('pagehide', function () {
    clearTimeout(saveTimer); saveNow(true);
    // 떠나기 전 오디오 버퍼·컨텍스트 명시적 해제 — 곡 전환마다 수백 MB 를 바로 반환(누수 방지 2026-07-14).
    try { if (player.destroy) player.destroy(); } catch (e) { /* 저장은 이미 됨 */ }
  });
  setInterval(function () { if (player.isPlaying()) saveNow(); }, 5000);

  /* ---- 공용 재생 박스 — 훅은 뷰들로 팬아웃 ---- */
  var transport = Transport.init({
    songId: songId,
    player: player,
    state: state, // 살아있는 객체 — 재할당 금지
    save: save,
    onTick: function (t, dur) { emit('tick', t, dur); },
    onSeek: function (t) { emit('seek', t); save(); },
    onLoopChange: function () { emit('loop'); },
    onPlayState: function (p) { emit('play', p); },
    onSyncChange: function (ms) { emit('sync', ms); },
  });

  // 화면 시간 = 표시 시계 단일 소스에 위임(transport.displayTime). 여기서 공식을 다시 조립하면
  // 진행바(transport)·타브와 갈라져 재생 중 어긋난다 — 반드시 한 함수만.
  function visualTime() {
    return transport.displayTime();
  }

  /* ---- 가사 소절 → 마디 분배(공용) — 단어별 '실제 시각'이 속한 마디에 배치(사용자 지시:
     박자에 맞게). 단어 시각이 없는 구버전/수동 수정 소절만 걸친 마디에 균등 폴백. ---- */
  function spreadLyrics(segments, timeToBar) {
    var byBar = {}; // bar -> {text, startIdxs(이 마디에서 시작), spanIdxs(걸침), manual}
    function barOf(b) {
      return byBar[b] = byBar[b] || { text: '', startIdxs: [], spanIdxs: [], manual: false };
    }
    function put(b, part, i, isStart, manual) {
      var cur = barOf(b);
      if (part) cur.text += (cur.text ? ' ' : '') + part;
      if (isStart && cur.startIdxs.indexOf(i) < 0) cur.startIdxs.push(i);
      if (cur.spanIdxs.indexOf(i) < 0) cur.spanIdxs.push(i);
      if (manual) cur.manual = true;
    }
    (segments || []).forEach(function (seg, i) {
      var b0 = Math.max(0, timeToBar(seg.s + 0.01));
      if (seg.words && seg.words.length && !seg.manual) {
        // 단어 실시각 배치 — 그 단어를 실제로 부르는 마디 아래에
        seg.words.forEach(function (w) {
          put(Math.max(0, timeToBar(w.s + 0.01)), w.w, i, false, false);
        });
        put(b0, '', i, true, false);
        return;
      }
      // 폴백: 걸친 마디들에 균등 분배(단어 시각 없음 — 구버전·직접 고친 소절)
      var b1 = Math.max(b0, timeToBar(Math.max(seg.s + 0.01, seg.e - 0.05)));
      var words = String(seg.text || '').split(/\s+/).filter(Boolean);
      var nBars = b1 - b0 + 1;
      for (var b = b0; b <= b1; b++) {
        var w0 = Math.round((b - b0) * words.length / nBars);
        var w1 = Math.round((b - b0 + 1) * words.length / nBars);
        put(b, words.slice(w0, w1).join(' '), i, b === b0, seg.manual);
      }
    });
    return byBar;
  }

  /* ---- 분석 메타(키·BPM·박·코드) — 배지·메트로놈은 셸이, 내용 소비는 각 뷰가 ---- */
  function showBadges(t) {
    if (t.status !== 'ready' || !t.bpm) return;
    if (t.key_json && t.key_json.label) {
      // 표기: 메이저 기준 + 마이너 병기 — 메이저 곡도 상대 단조 병기(사용자 지시 2026-07-10 재확인)
      // 예: 키 F (Dm) · 키 E (C#m). minor 필드 없는 구 데이터는 마이너 곡만 병기(폴백)
      var kd = t.key_json.display || t.key_json.label;
      var mn = t.key_json.minor || (t.key_json.mode === 'minor' ? t.key_json.label : '');
      if (mn) kd += ' (' + mn + ')';
      var k = document.getElementById('key-badge');
      k.textContent = '키 ' + kd + (t.key_json.manual ? ' (직접)' : ' (추정)');
      k.hidden = false;
    }
    var b = document.getElementById('bpm-badge');
    // 12/8 은 검출 bpm 이 셋잇단 펄스(3배) — 체감 박자(점4분)로 표시
    b.textContent = t.meter === '12/8'
      ? 'BPM ' + Math.round(t.bpm / 3) + ' (12/8·추정)'
      : 'BPM ' + Math.round(t.bpm) + ' (추정)';
    b.hidden = false;
  }
  function refreshMeta() {
    return fetch('/api/songs/' + songId + '/tab')
      .then(function (r) { return r.json(); })
      .then(function (t) {
        lastMeta = t;
        transport.setMeta(t); // 메트로놈·카운트인 게이트 + 실측 박
        showBadges(t);
        emit('meta', t);
        return t;
      });
  }

  /* ---- 키 직접 입력(사용자 요청 2026-07-10) — 배지 클릭 → 팝오버, 저장하면 코드도 재계산 ---- */
  (function wireKeyEditor() {
    var ed = document.getElementById('key-editor');
    var input = document.getElementById('key-input');
    if (!ed) return;
    document.getElementById('key-badge').addEventListener('click', function () {
      input.value = (lastMeta && lastMeta.key_json && lastMeta.key_json.manual)
        ? lastMeta.key_json.label : '';
      ed.hidden = false;
      input.focus();
    });
    function close() { ed.hidden = true; }
    function submit(label) {
      fetch('/api/songs/' + songId + '/key', {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: label }),
      }).then(function (r) {
        if (!r.ok) {
          return r.json().then(function (e) {
            alert(e.detail || '키를 저장하지 못했어요');
          });
        }
        close();
        refreshMeta(); // 배지·코드 악보·조판·믹서 스트립까지 새 키 기준으로
      });
    }
    document.getElementById('key-save').addEventListener('click', function () { submit(input.value.trim()); });
    document.getElementById('key-auto').addEventListener('click', function () { submit(''); });
    document.getElementById('key-cancel').addEventListener('click', close);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') submit(input.value.trim());
      if (e.key === 'Escape') close();
    });
  })();

  /* ---- BPM 확인·보정(사용자 요청 2026-07-10) — 배수 오검출 교정 + 인터넷 대조(Deezer) ---- */
  (function wireBpmEditor() {
    var ed = document.getElementById('bpm-editor');
    if (!ed) return;
    var cur = document.getElementById('bpm-current');
    var out = document.getElementById('bpm-lookup-result');
    function dispBpm(t) {
      if (!t || !t.bpm) return null;
      return Math.round(t.meter === '12/8' ? t.bpm / 3 : t.bpm);
    }
    function renderCurrent() {
      var t = lastMeta;
      var b = dispBpm(t);
      var adj = t && t.tempo_override;
      cur.textContent = b
        ? '검출 BPM ' + b + (adj === 'half' ? ' (절반 보정 적용됨)' : adj === 'double' ? ' (2배 보정 적용됨)' : '')
        : '아직 분석이 없어요';
      // 이미 보정된 방향은 비활성 — 같은 방향 중복 적용 방지(반대 방향 = 자동 복귀)
      document.getElementById('bpm-half').disabled = !b || adj === 'half';
      document.getElementById('bpm-double').disabled = !b || adj === 'double';
    }
    document.getElementById('bpm-badge').addEventListener('click', function () {
      renderCurrent();
      out.hidden = true;
      ed.hidden = false;
    });
    var pollTimer = null;
    function applyTempo(mode) {
      // 반대 방향 클릭 = 보정 해제(자동): half 상태에서 '2배로' = 원래대로
      var adj = lastMeta && lastMeta.tempo_override;
      var target = (adj === 'half' && mode === 'double') || (adj === 'double' && mode === 'half')
        ? 'auto' : mode;
      cur.textContent = '다시 계산 중… (몇 초)';
      document.getElementById('bpm-half').disabled = true;
      document.getElementById('bpm-double').disabled = true;
      // 실패/에러 시에도 버튼을 반드시 재활성화(코드검사 2026-07-17: 성공 경로만 renderCurrent 하던 stuck UI)
      function reenable() { document.getElementById('bpm-half').disabled = false; document.getElementById('bpm-double').disabled = false; }
      fetch('/api/songs/' + songId + '/tab', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tempo: target }),
      }).then(function () {
        clearInterval(pollTimer);
        pollTimer = setInterval(function () {
          refreshMeta().then(function (t) {
            if (t.status === 'ready') { clearInterval(pollTimer); renderCurrent(); }
            else if (t.status === 'error') {
              clearInterval(pollTimer);
              cur.textContent = t.error || '다시 계산에 실패했어요';
              reenable();
            }
          }).catch(function () { clearInterval(pollTimer); cur.textContent = '다시 계산 중 문제가 있었어요'; reenable(); });
        }, 2000);
      }).catch(function () { cur.textContent = '요청을 보내지 못했어요 — 다시 시도해 주세요'; reenable(); });
    }
    document.getElementById('bpm-half').addEventListener('click', function () { applyTempo('half'); });
    document.getElementById('bpm-double').addEventListener('click', function () { applyTempo('double'); });
    document.getElementById('bpm-lookup').addEventListener('click', function () {
      out.hidden = false;
      out.textContent = '인터넷에서 찾는 중…';
      fetch('/api/songs/' + songId + '/lookup')
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (!res.found) { out.textContent = res.reason || '찾지 못했어요'; return; }
          var mine = dispBpm(lastMeta);
          var who = (res.artist ? res.artist + ' — ' : '') + (res.title || '');
          if (!res.bpm) { out.textContent = '찾음: ' + who + ' · BPM 정보가 없는 곡이에요'; return; }
          var ref = Math.round(res.bpm);
          var line = '인터넷 참고: BPM ' + ref + ' (' + who + ')';
          if (mine) {
            var ratio = mine / ref;
            if (ratio > 1.7 && ratio < 2.3) line += ' — 검출(' + mine + ')이 2배로 보여요. "절반으로"를 눌러보세요';
            else if (ratio > 0.43 && ratio < 0.6) line += ' — 검출(' + mine + ')이 절반으로 보여요. "2배로"를 눌러보세요';
            else if (Math.abs(mine - ref) <= 3) line += ' — 검출(' + mine + ')과 일치해요';
            else line += ' — 검출은 ' + mine + ' (버전이 다를 수 있어요)';
          }
          out.textContent = line;
        })
        .catch(function () { out.textContent = '인터넷 조회에 실패했어요'; });
    });
    // 바깥 클릭 닫기 — 키 편집기와 같은 결
    document.addEventListener('click', function (e) {
      if (!ed.hidden && !ed.contains(e.target) && e.target.id !== 'bpm-badge') ed.hidden = true;
    });
  })();

  /* ---- 전체 가사 모달(사용자 요청 2026-07-11) — 믹서·타브·코드가 공유하는 하나의 가사 ---- */
  (function wireLyricsModal() {
    var overlay = document.getElementById('lyrics-modal');
    var btn = document.getElementById('btn-lyrics-view');
    if (!overlay || !btn) return;
    var list = document.getElementById('lyr-list');
    var pasteBox = document.getElementById('lyr-paste');
    var pasteText = document.getElementById('lyr-paste-text');
    var pasteToggle = document.getElementById('lyrics-paste-toggle');
    var saveBtn = document.getElementById('lyrics-save');
    var pasteMode = false;
    // 붙여넣기 모드: 목록(줄편집) 대신 통짜 텍스트 — 현재 가사를 채워 통째 교체 가능(사용자 요청 2026-07-17).
    function setPaste(on) {
      pasteMode = on;
      if (pasteBox) pasteBox.hidden = !on;
      list.hidden = on;
      if (on && pasteText) {
        var segs = (lastMeta && lastMeta.lyrics && lastMeta.lyrics.segments) || [];
        if (!pasteText.value.trim()) pasteText.value = segs.map(function (s) { return s.text; }).join('\n');
      }
      if (pasteToggle) pasteToggle.textContent = on ? '↩ 줄별 수정' : '가사 붙여넣기';
      if (saveBtn) saveBtn.textContent = on ? '붙여넣기 적용' : '저장';
    }
    if (pasteToggle) pasteToggle.addEventListener('click', function () { setPaste(!pasteMode); });
    // '인터넷에서 가사 찾기' — 이 곡 제목·가수로 검색창을 연다(사용자 선택 2026-07-17: 가사 찾기 도우미).
    var searchWeb = document.getElementById('lyr-search-web');
    if (searchWeb) searchWeb.addEventListener('click', function () {
      var tEl = document.querySelector('.practice-title');
      var aEl = document.querySelector('.practice-artist');
      var title = tEl ? ((tEl.childNodes[0] && tEl.childNodes[0].textContent) || tEl.textContent || '').trim() : '';
      title = title.split(/[|/]/)[0].replace(/\([^)]*\)|\[[^\]]*\]|【[^】]*】/g, ' ').trim(); // 채널·태그 노이즈 제거
      var artist = aEl ? aEl.textContent.trim() : '';
      var q = (title + ' ' + artist + ' 가사').replace(/\s+/g, ' ').trim();
      window.open('https://www.google.com/search?q=' + encodeURIComponent(q), '_blank', 'noopener');
    });
    on('meta', function (t) {
      var ly = t.lyrics;
      btn.hidden = !(ly && ly.status === 'ready' && ly.segments && ly.segments.length);
    });
    function open() {
      if (pasteText) pasteText.value = '';
      setPaste(false);  // 열 때는 항상 줄편집 모드로
      var segs = (lastMeta && lastMeta.lyrics && lastMeta.lyrics.segments) || [];
      list.innerHTML = '';
      segs.forEach(function (seg, i) {
        var row = document.createElement('div');
        row.className = 'lyr-row' + (seg.improv ? ' improv' : '');
        var mm = Math.floor(seg.s / 60), ss = Math.floor(seg.s % 60);
        // ♪ placeholder = 노래는 있는데 받아쓰기가 못 옮긴 애드립 자리(타이밍만 정확). 입력칸 비워 직접 쓰게 안내.
        var isPh = !!seg.placeholder;
        var tag = isPh
          ? '<span class="lyr-tag" title="여기서 노래(애드립)가 나와요. 공식 가사엔 없는 부분이니 들어보고 직접 적어주세요 — 위치는 맞춰뒀어요">애드립 ✎</span>'
          : (seg.improv ? '<span class="lyr-tag" title="공식 가사에 없는 즉흥 부분 — 받아쓰기 초안이에요. 들어보고 직접 고쳐주세요">즉흥?</span>' : '');
        row.innerHTML = '<span class="lyr-time" title="누르면 이 시각으로 이동">' +
          mm + ':' + String(ss).padStart(2, '0') + '</span>' +
          '<input type="text" maxlength="200" data-i="' + i + '" placeholder="' +
          (isPh ? '애드립 — 들으며 입력' : '') + '"' +
          (seg.manual ? ' class="manual"' : (seg.improv ? ' class="improv"' : '')) + '>' + tag;
        row.querySelector('input').value = isPh ? '' : seg.text;
        row.querySelector('.lyr-time').addEventListener('click', function () {
          player.seek(seg.s);
          emit('seek', seg.s);
        });
        list.appendChild(row);
      });
      document.getElementById('lyr-msg').textContent = segs.length + '소절';
      overlay.hidden = false;
    }
    function close() { overlay.hidden = true; }
    btn.addEventListener('click', open);
    document.getElementById('lyrics-modal-close').addEventListener('click', close);
    document.getElementById('lyrics-cancel').addEventListener('click', close);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.getElementById('lyrics-save').addEventListener('click', function () {
      var msg0 = document.getElementById('lyr-msg');
      if (pasteMode) {  // 붙여넣기 적용 — 통짜 텍스트를 곡에 배치
        var text = (pasteText && pasteText.value || '').trim();
        if (!text) { close(); return; }
        msg0.textContent = '적용 중…';
        fetch('/api/songs/' + songId + '/lyrics/paste', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: text }),
        }).then(function (r) { if (!r.ok) throw 0; return r.json(); })
          .then(function (res) {
            if (res && res.upgrading) {
              // 붙여넣기는 바로 반영됨(화면 유지) + 배경에서 즉흥(애드립)까지 잡아 얹는 중(가사 폴링이 자동 교체)
              msg0.textContent = '붙여넣었어요 — 배경에서 즉흥(애드립)까지 잡아 잠시 뒤 자동으로 더해져요';
              return refreshMeta().then(function () { setTimeout(close, 1600); });
            }
            return refreshMeta().then(close);
          })
          .catch(function () { msg0.textContent = '적용 중 문제가 있었어요 — 다시 시도해주세요'; });
        return;
      }
      var segs = (lastMeta && lastMeta.lyrics && lastMeta.lyrics.segments) || [];
      var changes = [];
      list.querySelectorAll('input[data-i]').forEach(function (inp) {
        var i = parseInt(inp.dataset.i, 10);
        if (segs[i] && inp.value.trim() !== segs[i].text) {
          changes.push({ index: i, text: inp.value.trim() }); // 빈칸 = 그 줄 삭제
        }
      });
      if (!changes.length) { close(); return; }
      changes.sort(function (a, b) { return b.index - a.index; }); // 내림차순 — 삭제가 인덱스를 안 밀게
      var msg = document.getElementById('lyr-msg');
      msg.textContent = '저장 중…';
      var chain = Promise.resolve();
      changes.forEach(function (c) {
        chain = chain.then(function () {
          return fetch('/api/songs/' + songId + '/lyrics', {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(c),
          });
        });
      });
      chain.then(function () { return refreshMeta(); }) // 세 화면 가사 표시 전부 갱신
        .then(function () { close(); })
        .catch(function () { msg.textContent = '저장 중 문제가 있었어요 — 다시 시도해주세요'; });
    });
  })();

  /* ---- 초기화: 설정 → 곡·상태 → 스템 → 복원 ---- */
  fetch('/api/settings').then(function (r) { return r.json(); }).then(function (d) {
    document.getElementById('device-label').textContent = d.device === 'gpu' ? 'GPU 모드' : 'CPU 모드';
  });

  var stateReadyResolve, readyResolve;
  var stateReady = new Promise(function (res) { stateReadyResolve = res; });
  var ready = new Promise(function (res) { readyResolve = res; });

  Promise.all([
    fetch('/api/songs/' + songId).then(function (r) { return r.json(); }),
    fetch('/api/songs/' + songId + '/state').then(function (r) { return r.json(); }),
  ]).then(function (res) {
    var song = res[0];
    Object.assign(state, res[1] || {});
    stateReadyResolve(state); // 스템 로딩을 기다리지 않는 복원(토글류)용
    return player.load(song.stems).then(function () {
      var dur = player.duration();
      document.getElementById('duration-label').textContent = transport.fmt(dur);
      if (state.position) player.seek(Math.min(state.position, dur - 0.5));
      Object.keys(state.volumes || {}).forEach(function (k) { player.setStemVolume(k, state.volumes[k]); });
      Object.keys(state.muted || {}).forEach(function (k) { player.setMute(k, state.muted[k]); });
      // 다중 솔로 복원 — 구형 저장값(문자열 하나)도 배열로 수용(하위호환, 마이그레이션 없이 읽기 변환)
      if (state.solo) state.solo = player.setSolos(state.solo);
      transport.restore(); // 배속·마스터·메트로놈·키·카운트인·A-B + 시간 표시
      // 구형 브라우저 폴백 중이면 정직 고지(쉬운 말) — 새 엔진에서만 악기 정확 동기
      if (player.isLegacy) {
        var lg = document.createElement('div');
        lg.className = 'legacy-engine-note';
        lg.textContent = '지금 브라우저는 오래된 방식으로 재생돼요. 최신 크롬·엣지에서는 악기 소리가 더 정확하게 맞습니다.';
        lg.style.cssText = 'margin:6px 12px;padding:6px 10px;background:#fff7e0;border:1px solid #e8d48a;border-radius:8px;font-size:13px;color:#6b5900;';
        var tp = document.querySelector('.transport') || document.body;
        tp.parentNode.insertBefore(lg, tp.nextSibling);
      }
      window.__playerReady = true;
      readyResolve(song);
    });
  }).catch(function (err) {
    // 스템·상태 로드 실패를 화면에 표시(안 하면 죽은 페이지 + 재생 버튼 무반응 — 하드 규칙 9)
    window.__playerLoadError = true;
    var box = document.createElement('div');
    box.className = 'load-error-note';
    box.textContent = '곡을 불러오지 못했어요 — 페이지를 새로고침하거나, 목록에서 다시 분석해주세요.';
    box.style.cssText = 'margin:12px;padding:10px 14px;background:#fdecec;border:1px solid #e6a6a6;border-radius:8px;font-size:14px;color:#8a1f1f;';
    var tp = document.querySelector('.transport') || document.body;
    if (tp.parentNode) tp.parentNode.insertBefore(box, tp); else document.body.appendChild(box);
    var dl = document.getElementById('duration-label');
    if (dl) dl.textContent = '불러오기 실패';
    if (window.console) console.error('chaebo load 실패:', err);
  });
  refreshMeta();

  /* ---- 뷰 전환 — 섹션 표시 전환 + pushState(주소 유지, 리로드 없음) ---- */
  function urlFor(name) {
    return '/songs/' + songId + '/' + (name === 'mixer' ? 'practice' : name);
  }
  function applyActive(name) {
    document.querySelectorAll('[data-view-section]').forEach(function (s) {
      s.hidden = s.dataset.viewSection !== name;
    });
    document.querySelectorAll('.view-switch-btn').forEach(function (b) {
      var onBtn = b.dataset.view === name;
      b.classList.toggle('active', onBtn);
      b.setAttribute('aria-selected', String(onBtn));
    });
  }
  applyActive(active); // 초기 뷰 버튼 활성 표시(템플릿은 섹션만 hidden 처리)

  function show(name, opts) {
    opts = opts || {};
    if (!views[name] || name === active) return;
    active = name;
    applyActive(name);
    if (!opts.nopush) history.pushState({ view: name }, '', urlFor(name));
    if (!inited[name]) {
      inited[name] = true;
      views[name].init();
    } else if (views[name].activate) {
      views[name].activate();
    }
    refreshMeta(); // 다른 뷰에서의 편집·재분석을 이어받기(단일 GET)
  }
  document.querySelectorAll('.view-switch-btn').forEach(function (b) {
    b.addEventListener('click', function (e) {
      if (e.metaKey || e.ctrlKey || e.shiftKey) return; // 새 탭/창 열기는 브라우저에
      e.preventDefault();
      show(b.dataset.view);
    });
  });
  window.addEventListener('popstate', function () {
    var m = location.pathname.match(/\/(practice|tab|chords)$/);
    var v = m ? (m[1] === 'practice' ? 'mixer' : m[1]) : 'mixer';
    if (v !== active) show(v, { nopush: true });
  });

  function registerView(name, module) {
    views[name] = module;
    if (name === active && !inited[name]) { // 초기 뷰는 파일 파스 직후 바로
      inited[name] = true;
      module.init();
    }
  }

  window.__player = player; // 검증 배터리 관례
  window.__setSyncMs = transport.setSyncMs;
  window.__shellView = function () { return active; }; // 배터리: 활성 뷰 단언

  /* ---- 코드 이름 → 키 기준 숫자 (밴드 넘버 방식, req-candidate-nondiatonic-guide 8-c) ----
     기준은 항상 메이저(마이너 곡은 나란한 장조) — 키 배지의 '메이저 기준 표시' 규칙과 동일.
     E키: E→1, A→4, C#m→6m, D→♭7, G/B→♭3/5. 키가 없거나 못 읽는 라벨은 그대로 돌려준다. */
  var _NUM_PC = { C: 0, 'C#': 1, Db: 1, D: 2, 'D#': 3, Eb: 3, E: 4, F: 5,
                  'F#': 6, Gb: 6, G: 7, 'G#': 8, Ab: 8, A: 9, 'A#': 10, Bb: 10, B: 11 };
  var _NUM_DEG = ['1', '♭2', '2', '♭3', '3', '4', '♯4', '5', '♭6', '6', '♭7', '7'];
  function chordNum(label, keyJson) {
    if (!keyJson || keyJson.tonic == null || !label) return label;
    var base = keyJson.mode === 'minor' ? (keyJson.tonic + 3) % 12 : keyJson.tonic;
    return String(label).split('/').map(function (part) {
      var m = /^([A-G][#b♯♭]?)([\s\S]*)$/.exec(part.trim());
      if (!m) return part;
      var pc = _NUM_PC[m[1].replace('♯', '#').replace('♭', 'b')];
      if (pc == null) return part;
      var suf = m[2];
      if (suf.charAt(0) === '7') suf = '⁷' + suf.slice(1); // C7→1⁷ ('17' 오독 방지, 내슈빌 위첨자 관례)
      return _NUM_DEG[(pc - base + 12) % 12] + suf;
    }).join('/');
  }

  /* ---- 빌린 코드(논 다이아토닉) 분류 — req-candidate-nondiatonic-guide §3.
     반환 null=다이아토닉(배지 없음) / {cls:'secondary'|'borrowed'|'passing'|'outside', ...}.
     분류는 근음+성질 기준(슬래시 베이스는 표시 문제라 무시). 키가 없으면 판정 안 함(null). */
  var _MAJ_QUAL = { 0: '', 2: 'm', 4: 'm', 5: '', 7: '', 9: 'm', 11: 'dim' };   // 메이저 다이아토닉
  var _AEO_QUAL = { 0: 'm', 2: 'dim', 3: '', 5: 'm', 7: 'm', 8: '', 10: '' };   // 같은으뜸음 단조(빌림 출처)
  function _parseChord(label) {
    var m = /^([A-G][#b♯♭]?)([^/]*)(?:\/.+)?$/.exec(String(label || '').trim());
    if (!m) return null;
    var pc = _NUM_PC[m[1].replace('♯', '#').replace('♭', 'b')];
    if (pc == null) return null;
    var suf = m[2] || '';
    var q = /^(dim|°|o(?![a-z]))/.test(suf) ? 'dim' : (/^m(?!aj)/.test(suf) ? 'm' : '');
    return { pc: pc, q: q, suf: suf };
  }
  function _majBase(keyJson) { return keyJson.mode === 'minor' ? (keyJson.tonic + 3) % 12 : keyJson.tonic; }
  function chordInfo(label, keyJson, nextLabel) {
    if (!keyJson || keyJson.tonic == null) return null;
    var c = _parseChord(label);
    if (!c) return null;
    if (/^sus/.test(c.suf)) return null;               // sus 는 장·단 판정 불가 — 배지 안 붙임(과분류 방지)
    var base = _majBase(keyJson);
    var iv = (c.pc - base + 12) % 12;
    if (_MAJ_QUAL[iv] != null && _MAJ_QUAL[iv] === c.q) return null;   // 다이아토닉
    var nx = nextLabel ? _parseChord(nextLabel) : null;
    var nxPc = nx ? nx.pc : null; // 다음 코드 근음 — 가이드의 '반음 접근' 팁을 실제 음이름으로 쓰기 위해
    if (c.q === '' && nx && (c.pc - nx.pc + 12) % 12 === 7 && nx.pc !== c.pc) {
      var niv = (nx.pc - base + 12) % 12;
      if (_MAJ_QUAL[niv] != null && _MAJ_QUAL[niv] === nx.q)           // 목표가 다이아토닉일 때만 V/x
        return { cls: 'secondary', base: base, pc: c.pc, q: c.q, target: String(nextLabel).split('/')[0], next: nxPc };
    }
    if (_AEO_QUAL[iv] != null && _AEO_QUAL[iv] === c.q) return { cls: 'borrowed', base: base, pc: c.pc, q: c.q, next: nxPc };
    if (c.q === 'dim') return { cls: 'passing', base: base, pc: c.pc, q: c.q, next: nxPc };
    return { cls: 'outside', base: base, pc: c.pc, q: c.q, next: nxPc };
  }

  /* ---- 가이드 문구(공용) — 흐름 타브·코드 악보가 같은 내용을 쓴다(뷰 간 세트 일관성, 사용자 지적).
     ★슬래시 코드는 베이스 음=빗금 아래 음(사용자 버그 지적 2026-07-23: G/B 에 '근음 G' 안내는 오답). */
  var _PCS_S = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
  var _PCS_F = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'Gb', 'G', 'Ab', 'A', 'Bb', 'B'];
  function pcName(pc, keyJson) {
    var flat = /b/.test((keyJson && (keyJson.display || keyJson.label)) || '');
    return (flat ? _PCS_F : _PCS_S)[((pc % 12) + 12) % 12];
  }
  var ND_TITLES = { borrowed: '다른 조에서 빌려온 코드 (추정)', secondary: '다음 코드로 끌어당기는 코드 (추정)',
                    passing: '반음 다리 코드 (추정)', outside: '조 밖 코드 (추정)' };
  function _escG(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;'); }
  function chordTones(nd, keyJson) { // 안전음(코드톤) 이름 — 상시 미니 힌트·가이드 공용
    return [pcName(nd.pc, keyJson), pcName(nd.pc + (nd.q === 'm' || nd.q === 'dim' ? 3 : 4), keyJson),
            pcName(nd.pc + (nd.q === 'dim' ? 6 : 7), keyJson)];
  }
  function chordGuideHtml(label, nd, keyJson) {
    var slash = String(label).split('/')[1] || null;
    var tones = chordTones(nd, keyJson);
    // 안전음 한 줄 — 슬래시면 베이스 음을 그 안에 담백하게(별도 설교 문장 금지 — 사용자 "너무 당연" 2026-07-26)
    var safe = '<p>안전한 음: 코드톤 <b>' + tones.join('·') + '</b>'
      + (slash ? ' (베이스는 빗금 아래 <b>' + _escG(slash) + '</b>)' : '') + '</p>';
    // 반음 접근 팁 — 다음 코드 근음이 반음 위/아래일 때만, 실제 음이름으로(추상 문구 금지 — 사용자 지적)
    var stepTip = '';
    if (nd.next != null) {
      var rel = (nd.next - nd.pc + 12) % 12;
      if (rel === 1 || rel === 11) {
        stepTip = '<p>다음 코드 근음 <b>' + pcName(nd.next, keyJson) + '</b>' + (rel === 1 ? '(반음 위)' : '(반음 아래)')
          + '라 <b>' + tones[0] + '→' + pcName(nd.next, keyJson) + '</b>로 붙여 가면 자연스러워요.</p>';
      }
    }
    var body = '';
    if (nd.cls === 'borrowed') {
      var clash = [4, 9, 11].map(function (i) { return pcName(nd.base + i, keyJson); }).join('·');
      var instead = [3, 8, 10].map(function (i) { return pcName(nd.base + i, keyJson); }).join('·');
      body = safe
        + '<p>이 마디 라인은 <b>' + pcName(nd.base, keyJson) + ' 마이너</b> 음들이 어울려요 — 곡 스케일의 '
        + clash + ' 대신 <b>' + instead + '</b>.</p>' + stepTip;
    } else if (nd.cls === 'secondary') {
      body = '<p><b>' + _escG(nd.target) + '</b>(으)로 가려는 임시 5도 코드예요.</p>' + safe
        + '<p>' + _escG(nd.target) + ' 근음으로 5도 내려가면 진행감이 살아요.</p>' + stepTip;
    } else if (nd.cls === 'passing') {
      body = safe + '<p>앞뒤 코드를 반음으로 잇는 코드예요 — 베이스도 근음 반음 이동으로 지나가면 돼요.</p>';
    } else {
      body = safe + stepTip;
    }
    return '<p class="cg-title">' + _escG(label) + ' — ' + ND_TITLES[nd.cls] + '</p>' + body
      + '<p class="cg-est">키·코드가 추정이라 판정이 틀릴 수 있어요</p>';
  }

  /* ---- 관용 진행 탐지 — 고유 코드 시퀀스(키 기준 도수, 슬래시 무시)에서 패턴 매칭 (req 8-b·8-e④).
     겹침은 긴 패턴 우선(완전 포함되는 짧은 span 제거 — 위종지가 4코드 루프 안에서 매번 울리는 것 방지). */
  var _PROGS = [
    { name: '1·5·6m·4', seq: [[0, ''], [7, ''], [9, 'm'], [5, '']], title: '4코드 루프 — 팝에서 가장 흔한 진행' },
    { name: '6m·4·1·5', seq: [[9, 'm'], [5, ''], [0, ''], [7, '']], title: '4코드 루프(돌린 형) — 애절한 시작' },
    { name: '1·6m·4·5', seq: [[0, ''], [9, 'm'], [5, ''], [7, '']], title: '50년대풍 진행 — 올드팝·두왑' },
    { name: '4·5·3m·6m', seq: [[5, ''], [7, ''], [4, 'm'], [9, 'm']], title: '왕도 진행 — 발라드·J/K-pop 단골' },
    { name: '1·6·2·5', seq: [[0, ''], [9, '*'], [2, 'm'], [7, '']], title: '1-6-2-5 돌림 — 6이 메이저면 2로 끌어당기는 임시 5도' },
    { name: '2·5·1', seq: [[2, 'm'], [7, ''], [0, '']], title: '2-5-1 — 재즈·마무리의 기본 문법' },
    // 위종지(5→6m)는 제외 — 실측 48건/패턴 37%(곡12 에만 11개)로 스팸(워십·팝에선 일상 진행).
    // 재도입하려면 '구간(섹션) 끝'에서만 울리는 조건부로(sections 데이터 결합) — req 8-e 기록.
  ];
  function findProgressions(chords, keyJson) {
    if (!keyJson || keyJson.tonic == null || !chords || !chords.length) return [];
    var base = _majBase(keyJson);
    var seq = [];
    chords.slice().sort(function (a, b) { return (a.bar - b.bar) || ((a.pos || 0) - (b.pos || 0)); })
      .forEach(function (c) {
        if (seq.length && seq[seq.length - 1].label === c.label) return;
        var p = _parseChord(c.label);
        seq.push({ label: c.label, iv: p ? (p.pc - base + 12) % 12 : null, q: p ? p.q : null, bar: c.bar });
      });
    var found = [];
    _PROGS.forEach(function (pg) {
      for (var i = 0; i + pg.seq.length - 1 < seq.length; i++) {
        var hit = true;
        for (var k = 0; k < pg.seq.length; k++) {
          var s = seq[i + k], want = pg.seq[k];
          if (s.iv !== want[0]) { hit = false; break; }
          if (want[1] === '*' ? s.q === 'dim' : s.q !== want[1]) { hit = false; break; } // '*'=m/메이저 둘 다
        }
        if (hit) found.push({ fromBar: seq[i].bar, toBar: seq[i + pg.seq.length - 1].bar, name: pg.name, title: pg.title, len: pg.seq.length });
      }
    });
    found.sort(function (a, b) { return (b.len - a.len) || (a.fromBar - b.fromBar); });
    var kept = [];
    found.forEach(function (f) {
      if (!kept.some(function (k) { return f.fromBar >= k.fromBar && f.toBar <= k.toBar; })) kept.push(f);
    });
    return kept.sort(function (a, b) { return a.fromBar - b.fromBar; });
  }

  return {
    chordNum: chordNum,
    chordInfo: chordInfo,
    chordGuideHtml: chordGuideHtml,
    chordTones: chordTones,
    findProgressions: findProgressions,
    songId: songId,
    player: player,
    state: state,
    transport: transport,
    save: save,
    on: on,
    show: show,
    spreadLyrics: spreadLyrics,
    active: function () { return active; },
    visualTime: visualTime,
    refreshMeta: refreshMeta,
    meta: function () { return lastMeta; },
    stateReady: stateReady,
    ready: ready,
    registerView: registerView,
    fmt: transport.fmt,
  };
})();
