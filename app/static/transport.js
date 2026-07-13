/* 공용 재생 박스 동작(믹서·타브·코드 악보) — 마크업은 _transport.html 1벌, 동작은 이 파일 1벌.
   (사용자 실증 2026-07-10: 화면마다 복제된 트랜스포트가 제각각 드리프트 — 구조적 공통화)

   사용:
     var transport = Transport.init({
       songId, player,
       state,                 // 살아있는 공유 상태 객체(모듈이 loopA 등도 여기에 기록)
       save(patch),           // 공유 상태 영속(병합 저장은 페이지 몫)
       onTick(t, dur),        // 프레임마다 페이지 고유 갱신(커서·파형·마디 하이라이트)
       onSeek(t),             // 트랜스포트發 이동 직후(시크바·화살표 키)
       onLoopChange(),        // A-B 변경 후(믹서 칩·오버레이 갱신)
       onPlayState(playing),  // 재생 상태 UI 페이지 확장(타브 재생 캡션 등)
       onSyncChange(ms),      // 싱크 보정 변경(커서 다시 그리기)
     });
   이후: transport.setMeta(tabMeta) — 분석 메타(bpm·meter·slots)로 메트로놈·카운트인 활성화
        transport.restore()  — 공유 상태(배속·마스터·메트로놈·키·카운트인·A-B) 복원 */
(function () {
  'use strict';

  function fmt(sec) {
    if (sec == null || isNaN(sec)) return '—:—';
    var m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return m + ':' + String(s).padStart(2, '0');
  }

  function init(ctx) {
    var player = ctx.player;
    var songId = ctx.songId;
    var state = ctx.state;
    var noop = function () {};
    var onTick = ctx.onTick || noop;
    var onSeek = ctx.onSeek || noop;
    var onLoopChange = ctx.onLoopChange || noop;
    var onPlayState = ctx.onPlayState || noop;
    var onSyncChange = ctx.onSyncChange || noop;

    var el = function (id) { return document.getElementById(id); };
    var playBtn = el('play-btn');
    var seekbar = el('seekbar');
    var speed = el('speed');
    var meta = null;      // 분석 메타(bpm·meter·slots) — 메트로놈·카운트인의 전제
    var syncMs = 0;
    var driftPerMin = 0;  // '점점 밀림' 보정(ms/분) — 곡 위치에 비례. 고정 syncMs 로 못 잡는 선형 드리프트용

    /* ---- 표시 시계(단일 소스) — 화면의 모든 위치 표시가 이 함수 하나만 읽는다:
       진행바·시간·믹서 playhead·타브 커서·코드·가사. (과거 실패: 이 공식이 shell·tab·transport
       3곳에서 각자 조립돼 재생 중 서로 120ms 어긋남 — 공통화=인스턴스 1개.)
       논리 위치(currentTime)는 seek·저장·A-B 루프·워크릿 스케줄 전용 — 여기에 표시 보정을 쓰면
       위치가 밀려 저장된다. 두 보정(워크릿 지연·사용자 싱크)은 재생 중에만 — 정지 땐 맞출 소리가 없다. */
    function displayTime() {
      if (!player.isPlaying()) return player.currentTime(); // 정지 = 논리 위치(저장·시크 값과 일치)
      // 재생 중 = '실제 들리는' 위치: getOutputTimestamp 로 하드웨어 출력지연(스피커·BT)을 자동 보정
      // + 스트레치 look-ahead(worklet). 남는 잔차(BT 추가지연 등)만 사용자 싱크(tap-to-sync)로 뺀다.
      var heard = player.heardTime ? player.heardTime() : player.currentTime();
      // '점점 밀림' 선형 보정: 곡이 진행될수록(currentTime↑) 커지는 보정. 측정(2026-07-13)으로 앱
      // 표시시계는 드리프트 0 확인 — 밀림은 실기기 출력경로라 고정 offset 으론 못 잡아 위치 비례 항 추가.
      // driftPerMin>0 = 진행할수록 화면을 앞당김(화면이 소리보다 뒤처질 때). 정지 중엔 0(위 early-return).
      var drift = driftPerMin ? driftPerMin * (player.currentTime() / 60) / 1000 : 0;
      return heard - syncMs / 1000 + drift;
    }

    /* ---- 재생/일시정지 + 카운트인 ---- */
    var countinBusy = false;
    function setPlayingUI(playing) {
      el('icon-play').style.display = playing ? 'none' : '';
      el('icon-pause').style.display = playing ? '' : 'none';
      playBtn.setAttribute('aria-label', playing ? '일시정지' : '재생');
      playBtn.setAttribute('aria-pressed', String(playing));
      onPlayState(playing);
    }
    playBtn.addEventListener('click', function () {
      if (countinBusy) return;
      if (player.isPlaying()) {
        player.pause(); setPlayingUI(false);
        ctx.save({ position: player.currentTime() });
        return;
      }
      var ci = el('countin-check');
      if (ci && ci.checked && meta && meta.bpm) { // 카운트인 — 체감 박자 4클릭(배속 반영)
        countinBusy = true;
        playBtn.disabled = true;
        var feel = (meta.meter === '12/8' ? meta.bpm / 3 : meta.bpm) * (player.rate || 1.0);
        player.countIn(feel, 4, function () {
          countinBusy = false;
          playBtn.disabled = false;
          player.play(); setPlayingUI(player.isPlaying());
        });
        return;
      }
      // isPlaying() 로 확인 — 스템이 안 실린(로드 실패) 상태면 play() 가 no-op 이라 아이콘이
      // 잘못 '재생 중'으로 바뀌지 않게(죽은 페이지에 거짓 재생 표시 방지, 적대 리뷰 확정)
      player.play(); setPlayingUI(player.isPlaying());
    });
    el('countin-check').addEventListener('change', function (e) {
      ctx.save({ countin: e.target.checked });
    });

    /* ---- 시간·시크바 (onTick 팬아웃) ---- */
    var seekbarDragging = false;
    seekbar.addEventListener('pointerdown', function () { seekbarDragging = true; });
    window.addEventListener('pointerup', function () { seekbarDragging = false; });
    seekbar.addEventListener('input', function () {
      var t = seekbar.value / 1000 * (player.duration() || 0);
      player.seek(t);
      el('time-now').textContent = fmt(t);
      onSeek(t);
    });
    player.onTick = function (t, dur) {
      var td = displayTime(); // 진행바·시간·커서 전부 같은 표시 시계(재생 중 커서와 어긋나지 않게)
      el('time-now').textContent = fmt(td);
      if (!seekbarDragging && dur) seekbar.value = Math.round(td / dur * 1000);
      onTick(td, dur);
    };
    // 곡 끝에서 엔진이 스스로 멈추면 재생 버튼 아이콘을 '재생'으로 되돌린다(고착 방지)
    player.onEnded = function () {
      if (countinBusy) return;
      setPlayingUI(false);
      ctx.save({ position: player.currentTime() });
    };

    /* ---- 배속 ---- */
    function applyRate(r) {
      r = Math.round(r * 100) / 100;
      player.setRate(r);
      speed.value = r;
      el('speed-badge').textContent = r + 'x';
      document.querySelectorAll('.speed-preset').forEach(function (b) {
        b.classList.toggle('active', parseFloat(b.dataset.speed) === r);
      });
      var warn = el('speed-warning'); // 극단 배속 품질 경고(REQ-PLAY-006) — 요소 있는 화면에서만
      if (warn) warn.hidden = r >= 0.6;
    }
    speed.addEventListener('input', function () {
      applyRate(parseFloat(speed.value));
      ctx.save({ rate: player.rate });
    });
    document.querySelectorAll('.speed-preset').forEach(function (b) {
      b.addEventListener('click', function () {
        applyRate(parseFloat(b.dataset.speed));
        ctx.save({ rate: player.rate });
      });
    });

    /* ---- 전체 음량 ---- */
    el('master-vol').addEventListener('input', function () {
      var v = this.value / 100;
      player.setMaster(v);
      el('master-badge').textContent = this.value + '%';
      ctx.save({ master: v });
    });

    /* ---- 메트로놈(실측 박 동기 + 세분) ---- */
    var metroRaw = null;
    var metroDiv = 'beat';
    function metroClicksPer(mode, meter) {
      var per = meter === '12/8'
        ? { beat: 1, e8: 3, e16: 6, tu: 3 }
        : { beat: 1, e8: 2, e16: 4, tu: 3 };
      return per[mode] || 1;
    }
    function applyMetroDiv() {
      if (!metroRaw) return;
      var n = metroClicksPer(metroDiv, (meta && meta.meter) || '4/4');
      var out = [];
      for (var i = 0; i < metroRaw.length; i++) {
        out.push(metroRaw[i]);
        if (n > 1 && metroRaw[i + 1] != null) {
          var step = (metroRaw[i + 1] - metroRaw[i]) / n;
          for (var k = 1; k < n; k++) out.push(metroRaw[i] + step * k);
        }
      }
      player.metroConfig(out, 4 * n, n);
    }
    el('metro-check').addEventListener('change', function (e) {
      player.setMetro(e.target.checked);
      ctx.save({ metro: e.target.checked });
    });
    el('metro-vol').addEventListener('input', function () {
      player.setMetroVol(this.value / 100);
      ctx.save({ metroVol: this.value / 100 });
    });
    el('metro-div').addEventListener('change', function (e) {
      metroDiv = e.target.value;
      applyMetroDiv();
      ctx.save({ metroDiv: metroDiv });
    });

    /* ---- 키(피치) — 시프트 스템 요청·폴링·리로드 ---- */
    var pitchSemi = 0;
    var pitchPoll = null;
    var pitchReq = 0;
    function pitchCaption(n, building) {
      var cap = el('key-caption');
      if (building) cap.textContent = '키 바꾸는 중… (처음 한 번만 몇십 초)';
      else if (n === 0) cap.textContent = '반음 단위 · 속도 불변 · 악보는 원 키';
      else cap.textContent = '재생 키 ' + (n > 0 ? '+' : '') + n + ' 반음 (악보는 원 키)';
    }
    function setPitch(n, skipSave) {
      n = Math.max(-12, Math.min(12, n));
      if (n === pitchSemi) return;
      pitchSemi = n;
      el('key-value').textContent = (n > 0 ? '+' : '') + n;
      if (!skipSave) ctx.save({ pitch: n });
      var req = ++pitchReq;
      clearInterval(pitchPoll);
      pitchCaption(n, true);
      function apply(stems) {
        player.reload(stems).then(
          function () { if (req === pitchReq) pitchCaption(n, false); },
          function () { if (req === pitchReq) el('key-caption').textContent = '키를 바꾸지 못했어요 — 잠시 후 다시 시도해주세요'; }
        );
      }
      fetch('/api/songs/' + songId + '/pitch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ semitones: n }),
      }).then(function (r) { return r.json(); }).then(function (res) {
        if (req !== pitchReq) return;
        if (res.ready) { apply(res.stems); return; }
        pitchPoll = setInterval(function () {
          fetch('/api/songs/' + songId + '/pitch?semitones=' + n)
            .then(function (r) { return r.json(); })
            .then(function (r2) {
              if (req !== pitchReq) return; // 내 인터벌은 setPitch 가 이미 정리 — 새 요청 것을 지우면 안 됨
              if (r2.ready) { clearInterval(pitchPoll); apply(r2.stems); }
              else if (r2.error) { // 실패 표면화 — 무한 '바꾸는 중' 방지
                clearInterval(pitchPoll);
                el('key-caption').textContent = r2.error;
                pitchSemi = 0;
                el('key-value').textContent = '0';
                ctx.save({ pitch: 0 });
              }
            });
        }, 1500);
      });
    }
    el('key-minus').addEventListener('click', function () { setPitch(pitchSemi - 1); });
    el('key-plus').addEventListener('click', function () { setPitch(pitchSemi + 1); });

    /* ---- A-B 루프 — 지정됨 표시(A 0:05 강조) 포함 ---- */
    function renderLoopUI() {
      var has = state.loopA != null && state.loopB != null;
      el('loop-indicator').hidden = !has;
      if (has) player.setLoop(state.loopA, state.loopB);
      else player.clearLoop();
      var ba = el('btn-loop-a'), bb = el('btn-loop-b');
      ba.classList.toggle('loop-set', state.loopA != null);
      ba.textContent = state.loopA != null ? 'A ' + fmt(state.loopA) : 'A 지점';
      bb.classList.toggle('loop-set', state.loopB != null);
      bb.textContent = state.loopB != null ? 'B ' + fmt(state.loopB) : 'B 지점';
      onLoopChange();
    }
    el('btn-loop-a').addEventListener('click', function () {
      state.loopA = player.currentTime();
      if (state.loopB != null && state.loopB <= state.loopA) state.loopB = null;
      renderLoopUI();
      ctx.save({ loopA: state.loopA, loopB: state.loopB });
    });
    el('btn-loop-b').addEventListener('click', function () {
      var t = player.currentTime();
      if (state.loopA == null || t <= state.loopA) return;
      state.loopB = t;
      renderLoopUI();
      ctx.save({ loopA: state.loopA, loopB: state.loopB });
    });
    el('btn-loop-clear').addEventListener('click', function () {
      state.loopA = state.loopB = null;
      if ('activeLoop' in state) state.activeLoop = null;
      renderLoopUI();
      ctx.save({ loopA: null, loopB: null, activeLoop: null });
    });

    /* ---- 소리-화면 싱크 보정(전역 설정) — ±1000ms(블루투스 지연 실사례가 300 초과, 2026-07-11) ---- */
    var SYNC_MAX = 1000;
    function showSync() {
      el('sync-value').textContent = syncMs + 'ms';
      var av = el('align-value'); if (av) av.textContent = syncMs + 'ms';
    }
    function saveSync() {
      fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sync_ms: syncMs }),
      });
      showSync();
      onSyncChange(syncMs);
    }
    function stepSync(d) {
      syncMs = Math.max(-SYNC_MAX, Math.min(SYNC_MAX, syncMs + d));
      showSync();
      onSyncChange(syncMs);
    }
    // 길게 누르면 연속 증감 — 400ms 후 60ms 간격, 놓을 때 한 번만 저장(PUT 폭주 방지)
    function wireSyncButton(id, d) {
      var btn = el(id);
      var delay = null, rep = null;
      function stop() {
        clearTimeout(delay); clearInterval(rep);
        delay = rep = null;
        saveSync();
      }
      btn.addEventListener('pointerdown', function (e) {
        e.preventDefault();
        stepSync(d);
        delay = setTimeout(function () {
          rep = setInterval(function () { stepSync(d); }, 60);
        }, 400);
        function up() {
          window.removeEventListener('pointerup', up);
          window.removeEventListener('pointercancel', up);
          stop();
        }
        window.addEventListener('pointerup', up);
        window.addEventListener('pointercancel', up);
      });
    }
    wireSyncButton('btn-sync-minus', -20);
    wireSyncButton('btn-sync-plus', 20);
    // 0으로 되돌리기 — 리셋 버튼 + 현재값 숫자 클릭(쉬운 한국어, G4)
    function resetSync() { syncMs = 0; saveSync(); }
    if (el('btn-sync-reset')) el('btn-sync-reset').addEventListener('click', resetSync);
    el('sync-value').addEventListener('click', resetSync);

    /* ---- '점점 밀림' 보정(전역) — 곡이 진행될수록 화면-소리가 선형으로 벌어지는 걸 상쇄.
       측정 근거(2026-07-13): 앱 표시시계는 드리프트 0, 밀림은 실기기 출력경로(WebView2/WASAPI 리샘플)라
       고정 싱크 하나론 못 잡음 → 위치 비례(ms/분) 보정을 사람이 한 번 맞추면 곡 내내 유지. onSyncChange
       로 커서 즉시 다시 그림(정지 중엔 표시시계가 논리위치라 무영향). ---- */
    var DRIFT_MAX = 120;
    function showDrift() {
      var dv = el('drift-value');
      if (dv) dv.textContent = (driftPerMin > 0 ? '+' : '') + (Math.round(driftPerMin * 10) / 10) + ' ms/분';
    }
    function saveDrift() {
      fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sync_drift_ms_per_min: driftPerMin }),
      });
      showDrift();
      onSyncChange(syncMs); // 커서 다시 그리기(값은 syncMs 재사용 훅 — 표시시계가 driftPerMin 도 읽음)
    }
    function stepDrift(d) {
      driftPerMin = Math.max(-DRIFT_MAX, Math.min(DRIFT_MAX, Math.round((driftPerMin + d) * 10) / 10));
      showDrift();
      onSyncChange(syncMs);
    }
    function wireDriftButton(id, d) {
      var btn = el(id);
      if (!btn) return;
      var delay = null, rep = null;
      function stop() { clearTimeout(delay); clearInterval(rep); delay = rep = null; saveDrift(); }
      btn.addEventListener('pointerdown', function (e) {
        e.preventDefault();
        stepDrift(d);
        delay = setTimeout(function () { rep = setInterval(function () { stepDrift(d); }, 120); }, 400);
        function up() {
          window.removeEventListener('pointerup', up);
          window.removeEventListener('pointercancel', up);
          stop();
        }
        window.addEventListener('pointerup', up);
        window.addEventListener('pointercancel', up);
      });
    }
    wireDriftButton('btn-drift-minus', -1);
    wireDriftButton('btn-drift-plus', 1);
    if (el('drift-value')) el('drift-value').addEventListener('click', function () { driftPerMin = 0; saveDrift(); });

    fetch('/api/settings').then(function (r) { return r.json(); }).then(function (s) {
      if (s && s.sync_ms != null) { syncMs = s.sync_ms; showSync(); onSyncChange(syncMs); }
      if (s && s.sync_drift_ms_per_min != null) { driftPerMin = s.sync_drift_ms_per_min; showDrift(); }
    });

    /* ---- 소리-화면 맞추기(캘리브레이션) — 딱딱 소리 + 점을 커서와 같은 시계로 몰아 정렬.
       점은 커서(displayTime)와 동일 규약으로 깜빡: heardCtx >= beat + syncMs. 그래서 점을 소리에
       맞추면 그 syncMs 가 곡 커서에도 그대로 맞는다. 자동 보정 위 잔차(블루투스 등)를 귀로 잡는 도구. */
    /* 리듬게임식: 막대가 가운데 선(타겟)을 지날 때 '딱' 소리가 나야 맞다(사용자 지시 2026-07-13:
       깜빡이는 점은 못 맞춤). 막대는 커서와 같은 시계(heardCtx − syncMs)로 움직여 beat 마다 가운데를
       지난다. 클릭은 heardCtx 격자(k·PERIOD)에 예약 — heardCtx 가 실제 출력 위치라 소리가 나는 순간이
       곧 heardCtx=k·PERIOD → syncMs=0 이면 막대-가운데와 소리가 일치, 잔차만 −/＋로. */
    var alignModal = el('sync-align-modal');
    if (alignModal) {
      var ALIGN_PERIOD = 1.0;
      var alignRAF = null, alignTimer = null, alignOscs = [], alignScheduled = 0;
      var calibLast = null;   // 2점 자동보정: 직전 맞춤 {p(곡위치s), X(그 위치의 유효 보정 ms)}
      var marker = el('sync-align-marker');
      var alignHeardCtx = function () {
        var ctx = player.ctx; if (!ctx) return 0;
        if (ctx.getOutputTimestamp) {
          try {
            var ts = ctx.getOutputTimestamp();
            if (ts && ts.contextTime > 0 && ts.performanceTime > 0)
              return ts.contextTime + (performance.now() - ts.performanceTime) / 1000; // 드리프트 추종
          } catch (e) { /* 폴백 */ }
        }
        return Math.max(0, ctx.currentTime - ((ctx.baseLatency || 0) + (ctx.outputLatency || 0)));
      };
      var alignSchedule = function () {
        var ctx = player.ctx; if (!ctx) return;
        var now = ctx.currentTime;
        var start = Math.max(alignScheduled + 0.001, Math.ceil((now + 0.12) / ALIGN_PERIOD) * ALIGN_PERIOD);
        for (var t = start; t < now + 1.3; t += ALIGN_PERIOD) {  // 짧은 look-ahead — 닫을 때 잔여 최소
          var osc = ctx.createOscillator(), g = ctx.createGain();
          osc.frequency.value = 1000;
          g.gain.setValueAtTime(0.0001, t);
          g.gain.exponentialRampToValueAtTime(0.5 * (player.masterVol || 1) + 0.0001, t + 0.004);
          g.gain.exponentialRampToValueAtTime(0.0001, t + 0.06);
          osc.connect(g); g.connect(ctx.destination);
          osc.start(t); osc.stop(t + 0.08);
          alignOscs.push(osc);
          alignScheduled = t;
        }
        if (alignOscs.length > 6) alignOscs = alignOscs.slice(-6);
      };
      var alignFrame = function () {
        var hc = alignHeardCtx();
        // 커서(displayTime)와 같은 규약. displayTime 은 heard - [syncMs - driftPerMin·(pos/60)]/1000 이므로
        // 지금 위치의 '유효 보정' = syncMs - driftPerMin·(pos/60). 막대도 이걸 반영해 커서와 일치시킨다.
        var effCorr = syncMs - driftPerMin * (player.currentTime() / 60);  // ms
        var dt = hc - effCorr / 1000;
        var frac = dt / ALIGN_PERIOD;
        var phase = frac - Math.round(frac);      // -0.5..+0.5, beat 에서 0(=가운데 타겟)
        if (marker) {
          marker.style.left = Math.max(0, Math.min(100, 50 + phase * 100)) + '%';
          marker.classList.toggle('hit', Math.abs(phase) < 0.06);
        }
        var mEl = el('align-measured');
        if (mEl) {
          var ctx = player.ctx;
          var hl = ctx ? (ctx.currentTime - hc) * 1000 : 0;   // 지금 추종 중인 실제 지연
          mEl.textContent = '자동 측정된 소리 지연 ' + Math.max(0, hl).toFixed(0) + 'ms · 내 조정 ' + syncMs + 'ms'
            + (driftPerMin ? ' · 밀림 ' + (Math.round(driftPerMin * 10) / 10) + 'ms/분(자동)' : '')
            + (calibLast ? ' · 한 번 더 맞추면 밀림 자동계산' : '');
        }
        alignRAF = requestAnimationFrame(alignFrame);
      };
      var stopAlign = function () {
        if (alignRAF) cancelAnimationFrame(alignRAF);
        if (alignTimer) clearInterval(alignTimer);
        alignRAF = alignTimer = null;
        // 예약된(미래) 클릭까지 확실히 끔 — 팝업 닫아도 띡띡 이어지던 문제(사용자 지적). disconnect 로
        // 출력 경로를 끊고(미래 예약도 무음) stop 도 시도.
        alignOscs.forEach(function (o) { try { o.disconnect(); } catch (e) {} try { o.stop(); } catch (e) {} });
        alignOscs = []; alignScheduled = 0;
      };
      var startAlign = function () {
        var ctx = player.ctx;
        if (!ctx) { alert('먼저 곡을 불러온 뒤 맞춰 주세요'); return false; }
        if (ctx.state === 'suspended') ctx.resume();
        alignOscs = []; alignScheduled = ctx.currentTime;
        alignSchedule();
        alignTimer = setInterval(alignSchedule, 250);
        alignRAF = requestAnimationFrame(alignFrame);
        return true;
      };
      /* 2점 자동 보정: 서로 다른 곡 위치에서 두 번 맞추면 '점점 밀림'(선형 드리프트) 속도를 자동 계산.
         한 번만 맞추면 예전처럼 고정 보정. 측정 근거: 밀림은 실기기 출력경로라 고정 offset 으론 못 잡음 —
         두 점의 유효보정 차이가 곧 드리프트 기울기다. 곡을 재생하며 맞춰야 위치가 벌어져 계산이 된다. */
      function captureCalibPoint() {
        var p = player.currentTime();
        var X = syncMs - driftPerMin * (p / 60);   // 지금 위치의 유효 보정(ms) — 방금 귀로 맞춘 값
        if (calibLast && Math.abs(p - calibLast.p) >= 15) {
          var b = (X - calibLast.X) / (p - calibLast.p);        // ms/초 (유효보정의 기울기)
          // corr(p)=syncMs - driftPerMin·(p/60) = a + b·p  →  driftPerMin = -60·b, syncMs = a
          driftPerMin = Math.max(-DRIFT_MAX, Math.min(DRIFT_MAX, Math.round(-60 * b * 10) / 10));
          syncMs = Math.max(-SYNC_MAX, Math.min(SYNC_MAX, Math.round(calibLast.X - b * calibLast.p)));
          showDrift(); saveDrift();
        }
        showSync(); saveSync();
        calibLast = { p: p, X: X };   // 다음 맞춤의 기준점(원래 X — 새 파라미터로도 이 점을 지남)
      }
      var closeAlign = function () { stopAlign(); alignModal.hidden = true; captureCalibPoint(); };
      el('btn-sync-align').addEventListener('click', function () {
        alignModal.hidden = false; showSync();
        if (!startAlign()) alignModal.hidden = true;
      });
      el('align-minus').addEventListener('click', function () { stepSync(-5); }); // 소리가 늦으면(막대가 먼저) 앞당김
      el('align-plus').addEventListener('click', function () { stepSync(5); });    // 소리가 빠르면 늦춤
      // 처음부터 다시: 고정 보정·밀림·2점 기준점 전부 초기화
      el('align-reset').addEventListener('click', function () {
        syncMs = 0; driftPerMin = 0; calibLast = null;
        showSync(); showDrift(); onSyncChange(syncMs);
      });
      el('align-done').addEventListener('click', closeAlign);
      alignModal.addEventListener('click', function (e) { if (e.target === alignModal) closeAlign(); });
    }

    /* ---- 공통 단축키 (REQ-PLAY-008 공통분) — 페이지 고유 키는 각 페이지에 ---- */
    var lastLoop = null; // L 토글용 — 해제 직전 구간 기억
    document.addEventListener('keydown', function (e) {
      var tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || tag === 'button') return;
      if (e.code === 'Space') { e.preventDefault(); playBtn.click(); }
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        e.preventDefault();
        var d = player.duration() || 0;
        player.seek(Math.max(0, Math.min(d, player.currentTime() + (e.key === 'ArrowLeft' ? -5 : 5))));
        el('time-now').textContent = fmt(player.currentTime());
        onSeek(player.currentTime());
      } else if (e.key === '[' || e.key === ']') {
        e.preventDefault();
        applyRate(Math.max(0.25, Math.min(2.0, player.rate + (e.key === ']' ? 0.05 : -0.05))));
        ctx.save({ rate: player.rate });
      } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        e.preventDefault();
        applyRate(Math.max(0.25, Math.min(2.0, player.rate + (e.key === 'ArrowUp' ? 0.05 : -0.05))));
        ctx.save({ rate: player.rate });
      } else if (e.key === 'l' || e.key === 'L') {
        e.preventDefault();
        if (state.loopA != null && state.loopB != null) {
          lastLoop = { a: state.loopA, b: state.loopB };
          el('btn-loop-clear').click();
        } else if (lastLoop) { // 해제 직전 구간 복귀(토글)
          state.loopA = lastLoop.a;
          state.loopB = lastLoop.b;
          renderLoopUI();
          ctx.save({ loopA: state.loopA, loopB: state.loopB });
        }
      }
    });

    /* ---- 메타·복원 ---- */
    function setMeta(t) {
      var ready = t && t.status === 'ready' && t.bpm;
      meta = ready ? t : null;
      // 박 정보 없으면 카운트인·메트로놈은 죽은 컨트롤 — 꺼두고 이유를 말한다
      ['countin-check', 'metro-check', 'metro-div'].forEach(function (id) {
        var c = el(id);
        if (!c) return;
        c.disabled = !ready;
        if (!ready && c.type === 'checkbox') c.checked = false;
        if (c.closest('label')) c.closest('label').title = ready ? '' : '타브 분석이 있어야 쓸 수 있어요';
      });
      if (!ready) { metroRaw = null; return; }
      var bs = (t.bar_slots === 48) ? 12 : 4;
      var beats = [];
      if (t.slots && t.slots.length) {
        for (var i = 0; i < t.slots.length; i += bs) beats.push(t.slots[i]);
      } else {
        for (var k = 0; k < 2000; k++) beats.push((t.offset || 0) + k * 60 / t.bpm);
      }
      metroRaw = beats;
      applyMetroDiv();
    }

    function restore() {
      el('time-total').textContent = fmt(player.duration());
      if (state.rate) applyRate(state.rate);
      if (state.master != null) {
        player.setMaster(state.master);
        el('master-vol').value = Math.round(state.master * 100);
        el('master-badge').textContent = Math.round(state.master * 100) + '%';
      }
      if (state.metroVol != null) {
        player.setMetroVol(state.metroVol);
        el('metro-vol').value = Math.round(state.metroVol * 100);
      }
      if (state.metroDiv) {
        metroDiv = state.metroDiv;
        el('metro-div').value = metroDiv;
        applyMetroDiv();
      }
      if (state.metro) {
        el('metro-check').checked = true;
        player.setMetro(true);
      }
      el('countin-check').checked = !!state.countin;
      if (state.pitch) setPitch(state.pitch, true);
      renderLoopUI();
      el('time-now').textContent = fmt(player.currentTime());
      // 재생바도 복원 위치로 — 안 그러면 위치는 복원됐는데 재생바만 0 에 있던 문제(사용자 지적)
      var dur0 = player.duration();
      if (dur0) el('seekbar').value = Math.round(player.currentTime() / dur0 * 1000);
    }

    return {
      fmt: fmt,
      applyRate: applyRate,
      setPlayingUI: setPlayingUI,
      setMeta: setMeta,
      restore: restore,
      renderLoopUI: renderLoopUI,
      displayTime: displayTime, // 표시 시계 단일 소스 — Shell.visualTime 이 이걸 위임
      syncMs: function () { return syncMs; },
      setSyncMs: function (v) { // 배터리·설정 화면용(저장 없이 즉시 반영)
        syncMs = Math.max(-SYNC_MAX, Math.min(SYNC_MAX, v));
        showSync();
        onSyncChange(syncMs);
      },
      isCountinBusy: function () { return countinBusy; },
    };
  }

  window.Transport = { init: init, fmt: fmt };
})();
