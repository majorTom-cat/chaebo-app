/* 근음 듣기 — 마이크로 지금 흐르는 곡의 근음(베이스 음)을 실시간 검출. 저역 집중 autocorrelation.
   마이크는 보안 컨텍스트(localhost 또는 HTTPS)에서만 — LAN HTTP 면 브라우저가 막는다(안내). */
(function () {
  var NOTES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
  var OPEN = [28, 33, 38, 43];               // 베이스 개방현 midi: E1 A1 D2 G2
  var STR_NAMES = ['G', 'D', 'A', 'E'];       // 지판 표시(위=G 얇은 줄, 아래=E)
  var noteEl = document.getElementById('t-note');
  var octEl = document.getElementById('t-oct');
  var hzEl = document.getElementById('t-hz');
  var dotEl = document.getElementById('t-dot');
  var msgEl = document.getElementById('t-msg');
  var btn = document.getElementById('t-toggle');

  /* ---- 지판 그리기 (위=G ~ 아래=E, 프렛 0~12) ---- */
  var fbCells = [];   // [string][fret] -> dot 요소
  (function buildFretboard() {
    var grid = document.getElementById('t-fretboard');
    // 헤더 행(프렛 번호)
    grid.appendChild(cell('', 'fb-strname'));
    for (var f = 0; f <= 12; f++) grid.appendChild(cell(f === 0 ? '개방' : String(f), 'fb-fnum'));
    for (var s = 0; s < 4; s++) {
      grid.appendChild(cell(STR_NAMES[s], 'fb-strname'));
      fbCells[s] = [];
      for (var fr = 0; fr <= 12; fr++) {
        var c = document.createElement('div');
        c.className = 'fb-cell' + (fr === 0 ? ' fb-open' : '');
        var dot = document.createElement('div'); dot.className = 'fb-dot';
        c.appendChild(dot); grid.appendChild(c);
        fbCells[s][fr] = dot;
      }
    }
    function cell(txt, cls) { var d = document.createElement('div'); d.className = cls; d.textContent = txt; return d; }
  })();
  function lightFretboard(pc) {   // pc = 0~11(없으면 -1) — 그 음이름의 모든 자리 켜기, 가장 낮은 자리=root
    var lowest = null;
    for (var s = 0; s < 4; s++) for (var fr = 0; fr <= 12; fr++) {
      var on = pc >= 0 && ((OPEN[3 - s] + fr) % 12 === pc);  // STR_NAMES 는 위=G(=OPEN[3]) 순
      fbCells[s][fr].classList.toggle('on', on);
      fbCells[s][fr].classList.remove('root');
      if (on) { var m = OPEN[3 - s] + fr; if (lowest === null || m < lowest.m) lowest = { s: s, fr: fr, m: m }; }
    }
    if (lowest) fbCells[lowest.s][lowest.fr].classList.add('root');
  }

  /* ---- 저역 집중 autocorrelation (40~400Hz 랙) ---- */
  function detect(buf, sr) {
    var SIZE = buf.length, rms = 0;
    for (var i = 0; i < SIZE; i++) rms += buf[i] * buf[i];
    rms = Math.sqrt(rms / SIZE);
    if (rms < 0.006) return null;             // 너무 조용
    var minLag = Math.max(2, Math.floor(sr / 400)), maxLag = Math.min(SIZE - 1, Math.ceil(sr / 40));
    var c0 = 0; for (var i = 0; i < SIZE; i++) c0 += buf[i] * buf[i]; c0 /= SIZE;
    var corr = new Float32Array(maxLag + 1);   // 랙별 정규화 자기상관(옥타브 가드에 재사용)
    var bestLag = -1, best = 0;
    for (var lag = minLag; lag <= maxLag; lag++) {
      var c = 0, n = SIZE - lag;
      for (var i = 0; i < n; i++) c += buf[i] * buf[i + lag];
      c /= n;
      corr[lag] = c;
      if (c > best) { best = c; bestLag = lag; }
    }
    if (bestLag < 0) return null;
    var conf = best / (c0 || 1);
    if (conf < 0.35) return null;             // 주기성 약함 = 근음 불명
    // 옥타브-다운 가드: 순수음은 2배 주기(한 옥타브 아래)를 집기 쉬움 —
    // 실제 기음(더 짧은 랙)에도 강한 주기성이 있으면 그쪽을 채택(2배 먼저, 없으면 3배).
    for (var div = 2; div <= 3; div++) {
      var sub = Math.round(bestLag / div);
      if (sub >= minLag && corr[sub] >= best * 0.85) { bestLag = sub; break; }
    }
    // 포물선 보간(정밀 주파수)
    var y1 = corr[bestLag - 1] || 0, y2 = corr[bestLag], y3 = corr[bestLag + 1] || 0;
    var denom = y1 - 2 * y2 + y3, shift = denom ? 0.5 * (y1 - y3) / denom : 0;
    if (shift < -1 || shift > 1) shift = 0;
    return { freq: sr / (bestLag + shift), conf: conf };
  }

  /* ---- 상태 ---- */
  var audioCtx = null, analyser = null, micStream = null, running = false, rafId = null, lastRun = 0;
  var hist = [];   // 최근 검출 음이름(pc) — 최빈값을 근음으로(흔들림 완화)

  function midiOf(freq) { return 69 + 12 * Math.log2(freq / 440); }

  function tick(ts) {
    if (!running) return;
    rafId = requestAnimationFrame(tick);
    if (ts - lastRun < 70) return;            // ~14fps (autocorrelation 부하 절감)
    lastRun = ts;
    var buf = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(buf);
    var res = detect(buf, audioCtx.sampleRate);
    if (res) {
      var m = midiOf(res.freq), mr = Math.round(m), pc = ((mr % 12) + 12) % 12;
      hist.push({ pc: pc, oct: Math.floor(mr / 12) - 1, freq: res.freq, cents: (m - mr) * 100 });
    } else {
      hist.push(null);
    }
    if (hist.length > 10) hist.shift();
    // 최빈 pc(유효 검출 중)
    var counts = {}, best = null;
    hist.forEach(function (h) { if (h) { counts[h.pc] = (counts[h.pc] || 0) + 1; } });
    var bestPc = -1, bestN = 0;
    Object.keys(counts).forEach(function (k) { if (counts[k] > bestN) { bestN = counts[k]; bestPc = +k; } });
    if (bestPc >= 0 && bestN >= 3) {
      var last = null; for (var i = hist.length - 1; i >= 0; i--) if (hist[i] && hist[i].pc === bestPc) { last = hist[i]; break; }
      noteEl.textContent = NOTES[bestPc];
      octEl.textContent = last ? (NOTES[bestPc] + last.oct) : '';
      hzEl.textContent = last ? (last.freq.toFixed(1) + ' Hz') : '';
      dotEl.style.opacity = '1';
      dotEl.style.left = Math.max(4, Math.min(96, 50 + (last ? last.cents : 0) * 0.5)) + '%';
      lightFretboard(bestPc);
    } else {
      noteEl.textContent = '–'; octEl.textContent = ''; hzEl.textContent = '';
      dotEl.style.opacity = '0'; lightFretboard(-1);
    }
  }

  function start() {
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      msgEl.className = 'tuner-msg warn';
      msgEl.innerHTML = '이 브라우저에선 마이크를 못 써요. 휴대폰·태블릿은 <b>보안 연결(https)</b>이 필요해요 — ' +
        '설정의 안내를 보거나, PC(chaebo 앱/localhost)에서는 바로 돼요.';
      return;
    }
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } })
      .then(function (stream) {
        micStream = stream;
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        var src = audioCtx.createMediaStreamSource(stream);
        var lp = audioCtx.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 500;  // 저역(근음) 집중
        var hp = audioCtx.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = 35;
        analyser = audioCtx.createAnalyser(); analyser.fftSize = 4096;
        src.connect(hp); hp.connect(lp); lp.connect(analyser);
        running = true; lastRun = 0; hist = [];
        btn.textContent = '■ 듣기 멈춤'; btn.classList.add('listening');
        msgEl.className = 'tuner-msg'; msgEl.textContent = '듣는 중… 곡을 스피커로 들려주세요.';
        rafId = requestAnimationFrame(tick);
      })
      .catch(function (e) {
        msgEl.className = 'tuner-msg warn';
        msgEl.textContent = '마이크 사용을 허용해 주세요. (' + (e && e.name || '거부됨') + ')';
      });
  }
  function stop() {
    running = false;
    if (rafId) cancelAnimationFrame(rafId);
    if (micStream) micStream.getTracks().forEach(function (t) { t.stop(); });
    if (audioCtx) audioCtx.close();
    micStream = null; audioCtx = null; analyser = null;
    btn.textContent = '🎧 듣기 시작'; btn.classList.remove('listening');
    noteEl.textContent = '–'; octEl.textContent = ''; hzEl.textContent = ''; dotEl.style.opacity = '0'; lightFretboard(-1);
    msgEl.className = 'tuner-msg'; msgEl.textContent = '';
  }
  btn.addEventListener('click', function () { running ? stop() : start(); });

  // 보안 컨텍스트가 아니면 미리 안내(버튼 누르기 전)
  if (!window.isSecureContext) {
    msgEl.className = 'tuner-msg';
    msgEl.innerHTML = '휴대폰·태블릿에서 마이크를 쓰려면 <b>보안 연결(https)</b>이 필요해요. PC에서는 바로 돼요.';
  }
})();
