/* SyncPlayer — 재생 엔진 래퍼 (SPEC 불변식: 이 인터페이스를 우회하는 재생 코드 금지).
   구현(2026-07-11 전환): Web Audio 단일 클럭 — AudioContext 1개 위에 스템당
   Signalsmith Stretch 워크릿(피치 유지 배속) + GainNode. 모든 노드가 같은 절대 시각의
   schedule 을 받아 샘플 단위로 함께 간다(SP-6 실측: 6노드 70초 렌더 비트 동일·클릭 0).
   구형 <audio> 엔진의 재정렬 보험(0.7x 편차 21~27ms 한계)은 이 아키텍처에선 존재하지 않는다.
   위치 계산 원칙: 재생 중 워크릿에 위치를 "묻지" 않는다(읽기 아티팩트 교훈) — 앵커(절대시각
   매핑)로 해석적으로 구하고, 모든 schedule 은 명시적 {input, rate, active, loop} 를 실어
   워크릿과 JS 가 같은 수식을 공유한다. 폴백: AudioWorklet 미지원 시 syncplayer-legacy.js. */
(function () {
  'use strict';

  var LEAD = 0.12;       // schedule 예약 리드(초) — 메시지 전달 여유(SP-6 시작지연 실측 21~32ms)
  var RAMP = 0.01;       // 게인 램프(초) — 솔로/뮤트/볼륨 전환 클릭 제거
  var vendorModule = null; // Signalsmith Stretch 모듈(1회 로드 캐시)

  function loadVendor() {
    if (!vendorModule) vendorModule = import('/static/vendor/signalsmith-stretch/SignalsmithStretch.mjs');
    return vendorModule.then(function (m) { return m.default; });
  }

  function SyncPlayer() {
    this.audios = [];      // {name, node(워크릿), gain}
    this.ctx = null;       // 공유 AudioContext(스템+메트로놈+카운트인 — 시계 1개)
    this.masterGain = null;
    this.rate = 1.0;
    this.solos = [];       // 다중 솔로(합집합) — DAW·Moises 표준
    this.muted = {};       // name -> bool
    this.volumes = {};     // name -> 0~1
    this.loopA = null;
    this.loopB = null;
    this.onTick = null;    // (currentTime, duration) 매 프레임
    this.onEnded = null;   // 곡 끝 자동 정지 시 1회 — 소비자가 재생 UI 를 되돌리게
    this._raf = null;
    this.masterVol = 1.0;
    this._dur = 0;
    this._playing = false;
    // 피치유지 배속 워크릿의 알고리즘 지연(초) — 실제 소리는 해석적 앵커보다 이만큼 '앞선' 입력을
    // 낸다(process: inputTime += inputLatency). 화면 커서가 소리보다 뒤처져 보이던 원인(사용자
    // 지적 2026-07-12). 로드 후 워크릿에서 실측값을 받아 화면 시간 보정에 쓴다(재생 중에만).
    this._latency = 0;
    // 위치 앵커: 곡 위치 = input + (ctx.currentTime - ctxT) × rate (루프 되감기는 읽을 때 적용)
    this._anchor = { ctxT: 0, input: 0, rate: 0 };
    this._realigns = 0;    // 구 배터리 계측 호환 — 단일 클럭이라 재정렬이 없다(항상 0)
    this._metroOn = false; // 메트로놈(Moises Smart Metronome 관례 — 곡 비트에 자동 동기)
    this._metroVol = 0.6;
    this._metroBeats = null;
    this._metroSched = {};
    this._metroTimer = null;
  }

  SyncPlayer.prototype._ensureCtx = function () {
    if (!this.ctx) {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      this.ctx = new Ctx();
      this.masterGain = this.ctx.createGain();
      this.masterGain.connect(this.ctx.destination);
    }
    if (this.ctx.state === 'suspended') this.ctx.resume();
    return this.ctx;
  };

  /* 로딩: 압축 스템을 통째로 받아 디코드 → 채널 배열을 워크릿에 transfer(복사 0회).
     디코드 원본(AudioBuffer)은 여기서 버려져 GC — 메모리는 워크릿 쪽 1벌만 남는다. */
  SyncPlayer.prototype.load = function (stems) {
    var self = this;
    var ctx = this._ensureCtx();
    var created = []; // 부분 성공 노드 추적 — 실패 시 정리(누수·유령 재생 방지, 적대 리뷰 확정)
    var p = loadVendor().then(function (SignalsmithStretch) {
      return Promise.all(Object.keys(stems).map(function (name) {
        return fetch(stems[name])
          .then(function (r) {
            if (!r.ok) throw new Error(name + ' 스템을 불러오지 못했어요(' + r.status + ')');
            return r.arrayBuffer();
          })
          .then(function (ab) { return ctx.decodeAudioData(ab); })
          .then(function (buf) {
            return SignalsmithStretch(ctx).then(function (node) {
              var ch0 = buf.getChannelData(0);
              var ch1 = buf.numberOfChannels > 1 ? buf.getChannelData(1) : ch0;
              // getChannelData 는 AudioBuffer 소유 메모리의 뷰 — 사본을 떠서 그 사본을 transfer
              var copies = [ch0.slice(0), ch1.slice(0)];
              return node.addBuffers(copies, copies.map(function (c) { return c.buffer; }))
                .then(function () {
                  var g = ctx.createGain();
                  node.connect(g);
                  g.connect(self.masterGain);
                  if (!(name in self.volumes)) self.volumes[name] = 1.0;
                  if (!(name in self.muted)) self.muted[name] = false;
                  var entry = { name: name, node: node, gain: g, duration: buf.duration };
                  created.push(entry);
                  return entry;
                });
            });
          });
      }));
    }).then(function (audios) {
      self.audios = audios;
      self._dur = 0;
      audios.forEach(function (a) { self._dur = Math.max(self._dur, a.duration); });
      self._applyAudible();
      // 워크릿 지연 실측(1회) — 화면 커서를 소리에 맞추는 보정값. 노드 1개면 충분(전부 동일 config).
      if (audios.length && audios[0].node.latency) {
        try {
          audios[0].node.latency().then(function (sec) {
            if (typeof sec === 'number' && sec >= 0 && sec < 1) self._latency = sec;
          });
        } catch (e) { /* 지연 조회 실패는 보정 0(기존 동작) */ }
      }
    }).catch(function (err) {
      // 스템 1개라도 실패하면 형제 노드가 그래프에 활성 잔존(워크릿 process 는 계속 돎) →
      // 끊고 버퍼 비워 GC 가능하게. audios 는 비워 소비자 play() 가 조용히 no-op 되도록.
      created.forEach(function (e) {
        try { e.node.dropBuffers(); } catch (x) { /* no-op */ }
        try { e.node.disconnect(); e.gain.disconnect(); } catch (x) { /* no-op */ }
      });
      self.audios = [];
      throw err; // 소비자(shell)가 쉬운 한국어 오류를 화면에 표시(하드 규칙 9)
    });
    // 내부 await 용(거부 흡수) — reload 가 초기 로드 완료를 기다리게 함(로딩 중 키 클릭 no-op 방지)
    this._loadDone = p.then(null, function () {});
    return p;
  };

  /* 스템 교체(키(피치) 변경 등) — 워크릿 노드는 재사용(버퍼만 교체: 노드 재생성은 워크릿 잔존 누수).
     위치·배속·솔로·음소거·볼륨·A-B 전부 보존. 연타 직렬화(체인) 유지 — 겹치면 상태 유실(적대 리뷰 실증) */
  SyncPlayer.prototype.reload = function (stems) {
    var self = this;
    var run = function () { return self._reloadNow(stems); };
    // .then(run, run): 직전 reload 가 성공했든 실패했든 이번 것을 실행 — 1회 실패가 체인을 영구
    // 오염(이후 모든 키 변경 무시)시키던 결함 교정(적대 리뷰 확정). 반환 프로미스의 거부는 호출부가 처리.
    this._reloadChain = (this._reloadChain || Promise.resolve()).then(run, run);
    return this._reloadChain;
  };
  SyncPlayer.prototype._reloadNow = function (stems) {
    var self = this;
    var ctx = this._ensureCtx();
    // 초기 로드 완료를 먼저 기다린다 — 로딩 중 키 클릭이 audios=[] 로 무음 no-op 되며 UI·저장만
    // 키 변경으로 표시되던 결함(정직 UI 위반) 교정. 로드 실패면 여기서도 노드가 없어 아래서 걸러진다.
    return (this._loadDone || Promise.resolve()).then(function () {
      if (!self.audios.length) throw new Error('아직 준비 중이에요 — 잠시 후 다시 시도해주세요');
      var byName = {};
      self.audios.forEach(function (a) { byName[a.name] = a; });
      // 1단계: 전 스템 fetch+decode 를 먼저 끝낸다(버퍼는 아직 안 건드림) — 하나라도 실패하면
      // 여기서 throw 되어 스템 간 키가 섞이는 일이 없다(원자적 교체, 적대 리뷰 확정). 옛 키는 계속 재생.
      return Promise.all(Object.keys(stems).map(function (name) {
        var a = byName[name];
        if (!a) return null; // 6종 고정 — 방어만
        return fetch(stems[name])
          .then(function (r) {
            if (!r.ok) throw new Error(name + ' 스템을 불러오지 못했어요(' + r.status + ')');
            return r.arrayBuffer();
          })
          .then(function (ab) { return ctx.decodeAudioData(ab); })
          .then(function (buf) {
            var ch0 = buf.getChannelData(0);
            var ch1 = buf.numberOfChannels > 1 ? buf.getChannelData(1) : ch0;
            return { a: a, copies: [ch0.slice(0), ch1.slice(0)], duration: buf.duration };
          });
      })).then(function (decoded) {
        // 2단계: 전부 성공 → 이제서야 상태 스냅샷 + 정지 + 버퍼 교체 + 복원
        var st = { t: self.currentTime(), playing: self.isPlaying() };
        self.pause();
        return Promise.all(decoded.map(function (d) {
          if (!d) return null;
          return d.a.node.dropBuffers().then(function () {
            return d.a.node.addBuffers(d.copies, d.copies.map(function (c) { return c.buffer; }));
          }).then(function () { d.a.duration = d.duration; });
        })).then(function () {
          self._dur = 0;
          self.audios.forEach(function (a) { self._dur = Math.max(self._dur, a.duration); });
          self._applyAudible();
          self.seek(st.t);
          if (st.playing) self.play();
        });
      });
    });
  };

  /* ---- 위치/전송: 모든 schedule 은 전 노드 동일 절대시각 + 명시적 전체 상태 ---- */

  // 루프 되감기 반영(읽기 전용) — 워크릿의 wrap(check: inputTime >= loopEnd)과 같은 수식
  SyncPlayer.prototype._wrap = function (t) {
    if (this.loopA != null && this.loopB != null && this.loopB > this.loopA && t >= this.loopB) {
      t = this.loopA + (t - this.loopA) % (this.loopB - this.loopA);
    }
    return t;
  };
  // ctxTime 시점의 곡 위치(래핑 적용)
  SyncPlayer.prototype._posAt = function (ctxTime) {
    var a = this._anchor;
    var t = a.input + Math.max(0, ctxTime - a.ctxT) * a.rate;
    return Math.max(0, Math.min(this._dur || Infinity, this._wrap(t)));
  };
  // 전 노드에 동일한 완전 상태를 예약하고 앵커를 그 시각으로 옮긴다
  SyncPlayer.prototype._schedAll = function (T, input, rate, active) {
    var obj = {
      output: T, input: input, rate: rate, active: active,
      loopStart: (this.loopA != null && this.loopB != null) ? this.loopA : 0,
      loopEnd: (this.loopA != null && this.loopB != null) ? this.loopB : 0,
    };
    this.audios.forEach(function (a) {
      a.node.schedule({
        output: obj.output, input: obj.input, rate: obj.rate, active: obj.active,
        loopStart: obj.loopStart, loopEnd: obj.loopEnd,
      });
    });
    this._anchor = { ctxT: T, input: input, rate: active ? rate : 0 };
  };

  SyncPlayer.prototype.duration = function () { return this._dur; };
  SyncPlayer.prototype.currentTime = function () {
    if (!this.ctx) return this._anchor.input;
    return this._posAt(this.ctx.currentTime);
  };
  SyncPlayer.prototype.isPlaying = function () { return this._playing; };
  // 화면 커서를 소리에 맞추기 위한 재생 지연(초) — 재생 중에만. 정지 중엔 0(위치가 정확).
  SyncPlayer.prototype.playbackLatency = function () { return this._playing ? this._latency : 0; };

  // 하드웨어 출력지연(스피커·블루투스: 버퍼→DAC→스피커)까지 반영한 '지금 실제로 들리는' ctx 시각.
  // getOutputTimestamp = 지금 스피커로 나가는 샘플의 ctx 시각(브라우저 측정값). 없으면 base+output 로 폴백.
  SyncPlayer.prototype._heardCtxTime = function () {
    var ctx = this.ctx;
    // '지금 실제로 스피커로 나가는' ctx 위치. 핵심: 블루투스 등은 시계가 시간이 지날수록 드리프트해
    // (고정 지연이 아니라 누적) — 고정 outputLatency 로는 못 잡는다. getOutputTimestamp 로 실제 출력
    // 위치를 읽고 그 이후 경과(월클럭)만큼 투영해 드리프트까지 추종한다(리서치: Adenot·WebAudio 스펙).
    var raw = null;
    if (ctx.getOutputTimestamp) {
      try {
        var ts = ctx.getOutputTimestamp();
        if (ts && ts.contextTime > 0 && ts.performanceTime > 0) {
          raw = ts.contextTime + (performance.now() - ts.performanceTime) / 1000;
        }
      } catch (e) { /* 폴백 */ }
    }
    if (raw == null) {  // 폴백: 고정 출력지연(드리프트 추종은 못 하지만 오프셋은 반영). Windows 0 이면 무보정.
      raw = ctx.currentTime - ((ctx.baseLatency || 0) + (ctx.outputLatency || 0));
    }
    raw = Math.max(0, Math.min(ctx.currentTime, raw));  // 미래로는 안 넘게
    // EMA 평활 — getOutputTimestamp 의 Chrome/WebView2 bounce(WebAudio #2461) 를 눌러 커서 떨림 방지.
    // 큰 점프(시크·재생 시작)는 즉시 반영, 작은 요동만 평활(느린 드리프트 추종은 유지).
    if (this._heardEma == null || Math.abs(raw - this._heardEma) > 0.4) this._heardEma = raw;
    else this._heardEma += (raw - this._heardEma) * 0.15;
    return this._heardEma;
  };
  // 화면 커서가 가리켜야 할 '들리는' 곡 위치 = 들리는 ctx시각의 곡위치 + 스트레치 look-ahead(_latency).
  // (워크릿이 입력을 IL+OL 앞서 읽으므로 +_latency, 하드웨어 출력지연은 _heardCtxTime 이 자동으로 뺌.)
  // 정지 중엔 논리 위치(currentTime) — 소리가 없어 보정 대상 없음.
  SyncPlayer.prototype.heardTime = function () {
    if (!this.ctx || !this._playing) return this.currentTime();
    return this._posAt(this._heardCtxTime()) + this._latency;
  };

  SyncPlayer.prototype.play = function () {
    if (!this.audios.length) return;
    var ctx = this._ensureCtx();
    var T = ctx.currentTime + LEAD;
    var start = this._posAt(T);
    // 곡 끝에서 재생 = 처음부터(구 <audio> 엔진의 ended→play 규약 유지 — 안 하면 첫 프레임에
    // 즉시 재-정지되어 재생 버튼이 죽는 데드엔드, 적대 리뷰 확정). 루프 중이면 그대로.
    if (this._dur && this.loopA == null && start >= this._dur - 0.02) start = 0;
    this._schedAll(T, start, this.rate, true);
    this._playing = true;
    this._startLoop();
  };

  SyncPlayer.prototype.pause = function () {
    if (this.ctx && this.audios.length) {
      var T = this.ctx.currentTime + 0.03;
      var pos = this._posAt(T);
      this._schedAll(T, pos, this.rate, false);
    }
    this._playing = false;
    this._stopLoop();
  };

  SyncPlayer.prototype.seek = function (t) {
    t = Math.max(0, Math.min(this._dur || 0, t || 0));
    if (this._playing && this.ctx) {
      var T = this.ctx.currentTime + 0.08;
      this._schedAll(T, t, this.rate, true);
    } else {
      // 정지 중엔 앵커만 — 다음 play() 가 명시적 input 으로 워크릿을 데려간다
      this._anchor = { ctxT: this.ctx ? this.ctx.currentTime : 0, input: t, rate: 0 };
    }
  };

  SyncPlayer.prototype.setRate = function (r) {
    this.rate = r; // 피치 유지는 알고리즘 본질(REQ-PLAY-002) — 별도 플래그 없음
    if (this._playing && this.ctx) {
      var T = this.ctx.currentTime + 0.08;
      this._schedAll(T, this._posAt(T), r, true);
    }
  };

  SyncPlayer.prototype.setLoop = function (a, b) {
    this.loopA = a;
    this.loopB = b;
    this._syncLoopToNodes();
  };
  SyncPlayer.prototype.clearLoop = function () {
    // 해제 전에 현재 위치를 래핑 값으로 고정 — 안 그러면 미래 위치로 튄다
    var pos = this.currentTime();
    this.loopA = this.loopB = null;
    if (this.ctx) this._anchor = { ctxT: this.ctx.currentTime, input: pos, rate: this._anchor.rate };
    this._syncLoopToNodes();
  };
  SyncPlayer.prototype._syncLoopToNodes = function () {
    if (!this.ctx || !this.audios.length) return;
    var T = this.ctx.currentTime + 0.05;
    this._schedAll(T, this._posAt(T), this.rate, this._playing);
  };

  /* ---- 솔로/뮤트/볼륨: GainNode 램프(클릭 제거) ---- */

  SyncPlayer.prototype.setStemVolume = function (name, v) {
    this.volumes[name] = v;
    this._applyAudible();
  };
  SyncPlayer.prototype.setSolo = function (name) { // 토글, 합집합
    var i = this.solos.indexOf(name);
    if (i >= 0) this.solos.splice(i, 1);
    else this.solos.push(name);
    this._applyAudible();
    return this.solos.slice();
  };
  SyncPlayer.prototype.setSolos = function (arr) { // 복원용 — 구형 저장값(문자열)도 수용
    this.solos = Array.isArray(arr) ? arr.slice() : (arr ? [arr] : []);
    this._applyAudible();
    return this.solos.slice();
  };
  SyncPlayer.prototype.setMute = function (name, on) {
    this.muted[name] = on;
    this._applyAudible();
  };
  SyncPlayer.prototype.setMaster = function (v) {
    this.masterVol = Math.max(0, Math.min(1, v));
    this._applyAudible();
  };

  // 배터리·화면용 상태 질의(오디오 경로의 실제 게인 목표와 동일한 계산)
  SyncPlayer.prototype.audible = function (name) {
    return this.solos.length ? this.solos.indexOf(name) >= 0 : !this.muted[name];
  };
  SyncPlayer.prototype.stemGain = function (name) {
    return this.audible(name) ? Math.max(0, Math.min(1, (this.volumes[name] || 0) * this.masterVol)) : 0;
  };

  SyncPlayer.prototype._applyAudible = function () {
    var self = this;
    var now = this.ctx ? this.ctx.currentTime : 0;
    this.audios.forEach(function (a) {
      var target = self.stemGain(a.name);
      if (self.ctx) a.gain.gain.setTargetAtTime(target, now, RAMP);
      else a.gain.gain.value = target;
    });
  };

  /* ---- 메트로놈 — 공유 컨텍스트(스템과 같은 시계)에 클릭 예약 ---- */
  SyncPlayer.prototype.metroConfig = function (beatTimes, accentEvery, subEvery) {
    this._metroBeats = beatTimes && beatTimes.length ? beatTimes : null;
    this._metroAccent = accentEvery || 4;
    this._metroSubEvery = subEvery || 1;
    this._metroSched = {};
  };
  SyncPlayer.prototype.setMetro = function (on) {
    this._metroOn = !!on;
    if (on) this._ensureCtx();
  };
  SyncPlayer.prototype.setMetroVol = function (v) {
    this._metroVol = Math.max(0, Math.min(1, v));
  };
  SyncPlayer.prototype._metroTick = function () {
    if (!this._metroOn || !this._metroBeats || !this.ctx || !this.isPlaying()) return;
    var now = this.currentTime();
    var horizon = now + 0.35 * this.rate;
    var beats = this._metroBeats;
    var lo = 0, hi = beats.length - 1;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (beats[mid] <= now) lo = mid + 1; else hi = mid; }
    for (var i = lo; i < beats.length && beats[i] <= horizon; i++) {
      if (this._metroSched[i]) continue;
      this._metroSched[i] = true;
      var dt = (beats[i] - now) / this.rate;
      var t0 = this.ctx.currentTime + Math.max(0.01, dt);
      var osc = this.ctx.createOscillator();
      var g = this.ctx.createGain();
      var freq = 880;
      if (i % this._metroAccent === 0) freq = 1318;
      else if (this._metroSubEvery > 1 && i % this._metroSubEvery !== 0) freq = 660;
      osc.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, t0);
      // '전체 음량' 라벨 약속 — 클릭도 마스터를 따른다(리뷰 확정: 음량 0 인데 클릭만 울림)
      g.gain.exponentialRampToValueAtTime(0.6 * this._metroVol * this.masterVol + 0.0001, t0 + 0.004);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.07);
      osc.connect(g);
      g.connect(this.ctx.destination);
      osc.start(t0);
      osc.stop(t0 + 0.1);
    }
    var self = this;
    Object.keys(this._metroSched).forEach(function (k) {
      if (beats[k] < now - 0.5 || beats[k] > horizon + 1.0) delete self._metroSched[k];
    });
  };

  SyncPlayer.prototype._startLoop = function () {
    var self = this;
    this._stopLoop();
    function frame() {
      var t = self.currentTime();
      // 곡 끝(루프 없을 때): 해석적 위치는 스스로 멈추지 않는다 — 여기서 정지시킨다
      if (self._playing && self.loopA == null && self._dur && t >= self._dur) {
        self.pause();
        self._anchor.input = self._dur;
        t = self._dur;
        // 내부 자동 정지를 소비자에 알려 재생 버튼 아이콘을 되돌린다(안 하면 '재생 중'으로 고착)
        if (self.onEnded) self.onEnded();
      }
      if (self.onTick) self.onTick(t, self.duration());
      if (self._playing) self._raf = requestAnimationFrame(frame);
    }
    this._raf = requestAnimationFrame(frame);
    // A-B 루프는 워크릿 네이티브(샘플 정확) — 구형 엔진의 rAF seek 되감기·드리프트 보험 타이머는 없다
    if (!this._metroTimer) {
      this._metroTimer = setInterval(function () { self._metroTick(); }, 100);
    }
  };

  SyncPlayer.prototype._stopLoop = function () {
    if (this._raf) cancelAnimationFrame(this._raf);
    if (this._metroTimer) clearInterval(this._metroTimer);
    this._raf = this._metroTimer = null;
  };

  /* 카운트인 — 재생 전 예비박 클릭(첫 박 높은음). 공유 컨텍스트 재사용(닫지 않는다!) */
  SyncPlayer.prototype.countIn = function (bpm, beats, onDone) {
    var ctx;
    try { ctx = this._ensureCtx(); } catch (e) { onDone(); return; }
    var beat = 60 / (bpm || 100);
    var vol = 0.5 * this.masterVol + 0.0001;
    for (var i = 0; i < beats; i++) {
      var osc = ctx.createOscillator();
      var g = ctx.createGain();
      osc.frequency.value = i === 0 ? 1318 : 880;
      var t0 = ctx.currentTime + i * beat;
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(vol, t0 + 0.005);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.12);
      osc.connect(g);
      g.connect(ctx.destination);
      osc.start(t0);
      osc.stop(t0 + 0.15);
    }
    setTimeout(onDone, Math.round(beats * beat * 1000));
  };

  // AudioWorklet 미지원(구형 브라우저) → <audio> 폴백 엔진(syncplayer-legacy.js 가 먼저 로드됨)
  var supported = !!(window.AudioContext && window.AudioWorklet);
  window.SyncPlayer = supported ? SyncPlayer : window.SyncPlayerLegacy;
})();
