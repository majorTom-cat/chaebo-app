/* SyncPlayerLegacy — 구형 브라우저 폴백(AudioWorklet 미지원 시에만).
   본 엔진은 syncplayer.js(Web Audio 단일 클럭). 이 파일은 스템당 <audio> 구현의 보존본:
   6개 요소가 각자 시계라 배속에서 편차가 자라며(0.7x 실측 21~27ms), 250ms 재정렬 보험으로
   버텼던 라인이다. 유지보수하지 말 것 — 폴백 외 용도 금지. */
(function () {
  'use strict';

  function SyncPlayer() {
    this.audios = [];      // {name, el}
    this.master = null;    // 기준 시계 = 첫 스템
    this.rate = 1.0;
    this.solos = [];       // 다중 솔로(합집합) — DAW·Moises 표준(2026-07-11, 단일→Set)
    this.muted = {};       // name -> bool
    this.volumes = {};     // name -> 0~1
    this.loopA = null;
    this.loopB = null;
    this.onTick = null;    // (currentTime, duration) 매 프레임
    this._raf = null;
    this._driftTimer = null;
    this.masterVol = 1.0;  // 마스터 볼륨(스템 볼륨에 곱해짐 — Songsterr 10 관례) ※ this.master = 기준 요소
    this._metroOn = false; // 메트로놈(Moises Smart Metronome 관례 — 곡 비트에 자동 동기)
    this._metroVol = 0.6;
    this._metroBeats = null; // 곡 시간축의 박 시각 배열(동적 그리드에서 파생 — 템포 흔들림 추종)
    this._metroCtx = null;
    this._metroSched = {};   // 최근 예약한 박 인덱스(중복 예약 방지)
    this._metroTimer = null;
  }

  /* 스템을 Blob 으로 완전 수신 후 메모리에서 재생.
     이유(실곡 실측): <audio src=서버URL> 6개가 브라우저의 호스트당 동시연결 한도(6)를
     전부 점유해 같은 서버로 가는 모든 fetch(자동 저장·보정 저장·폴링)가 무기한 대기.
     Blob 은 다운로드 동안만 연결을 쓰고 반환 — 풀이 항상 비고 seek 도 즉시다. */
  SyncPlayer.prototype.load = function (stems) {
    var self = this;
    return Promise.all(Object.keys(stems).map(function (name) {
      return fetch(stems[name])
        .then(function (r) {
          if (!r.ok) throw new Error(name + ' 스템을 불러오지 못했어요(' + r.status + ')');
          return r.blob();
        })
        .then(function (bl) {
          var el = new Audio(URL.createObjectURL(bl));
          el.preload = 'auto';
          el.preservesPitch = true;
          self.volumes[name] = 1.0;
          self.muted[name] = false;
          return new Promise(function (resolve) {
            if (el.readyState >= 1) return resolve({ name: name, el: el });
            el.addEventListener('loadedmetadata', function () {
              resolve({ name: name, el: el });
            }, { once: true });
          });
        });
    })).then(function (audios) {
      self.audios = audios;
      self.master = audios[0].el;
      // 파이프라인 워밍업: blob 오디오는 첫 재생 시동이 간헐적으로 1초+ 지연됨(실측 재발).
      // 무음(muted)으로 60ms 재생 후 되감기 — 이후 play() 는 즉시 시작된다.
      return Promise.all(audios.map(function (a) {
        a.el.muted = true;
        return a.el.play()
          .then(function () { return new Promise(function (r) { setTimeout(r, 60); }); })
          .catch(function () {})
          .then(function () {
            a.el.pause();
            a.el.currentTime = 0.001;
            a.el.muted = false;
          });
      }));
    });
  };

  /* 스템 교체(키(피치) 변경 등) — 위치·배속·솔로·음소거·볼륨·A-B 전부 보존한 채 다시 로드.
     직렬화: 연타로 reload 가 겹치면 유령 오디오·상태 유실(적대 리뷰 확정) — 체인으로 순차 실행 */
  SyncPlayer.prototype.reload = function (stems) {
    var self = this;
    this._reloadChain = (this._reloadChain || Promise.resolve())
      .then(function () { return self._reloadNow(stems); });
    return this._reloadChain;
  };
  SyncPlayer.prototype._reloadNow = function (stems) {
    var self = this;
    var st = {
      t: this.currentTime(), rate: this.rate, playing: this.isPlaying(),
      solos: this.solos.slice(), muted: JSON.parse(JSON.stringify(this.muted)),
      volumes: JSON.parse(JSON.stringify(this.volumes)),
      loopA: this.loopA, loopB: this.loopB,
    };
    this.pause();
    this.audios.forEach(function (a) {
      try { URL.revokeObjectURL(a.el.src); } catch (e) { /* no-op */ }
      a.el.src = '';
    });
    this.audios = [];
    return this.load(stems).then(function () {
      self.rate = st.rate;
      self.audios.forEach(function (a) { a.el.preservesPitch = true; a.el.playbackRate = st.rate; });
      self.volumes = st.volumes;
      self.muted = st.muted;
      self.solos = st.solos;
      self._applyAudible();
      self.loopA = st.loopA;
      self.loopB = st.loopB;
      self.seek(st.t);
      if (st.playing) self.play();
    });
  };

  SyncPlayer.prototype.duration = function () {
    return this.master ? this.master.duration || 0 : 0;
  };
  SyncPlayer.prototype.currentTime = function () {
    return this.master ? this.master.currentTime : 0;
  };
  SyncPlayer.prototype.isPlaying = function () {
    return !!this.master && !this.master.paused;
  };

  SyncPlayer.prototype.play = function () {
    var self = this;
    // 시작 정렬(2026-07-11): 스템별 play() 시동 지연이 달라 첫 프레임부터 어긋남 —
    // 전 스템 시동 완료 후 master 기준 1회 재정렬
    Promise.all(this.audios.map(function (a) {
      var p = a.el.play();
      return p && p.catch ? p.catch(function () {}) : Promise.resolve();
    })).then(function () { self._alignToMaster(true); }); // 시작 정렬(실측: 이후 첫 편차 0.2ms)
    this._startLoop();
  };

  /* master 기준 재정렬. 실측(2026-07-11, 이 노트북): 재생 중 currentTime 비교는 요소별 보고
     시점 차이로 ~40ms 가 '보이지만' 정지 실측은 5.4ms — 낮은 임계는 오탐 재정렬 진동(719회/분)을
     만든다. → 임계 30ms + 재정렬 후 1.2초 쿨다운(25ms 는 재정렬 폭풍 77회/분 실증)(seek 정착 대기·진동 차단). 시작 정렬은 임계 무관 강제. */
  SyncPlayer.prototype._alignToMaster = function (force) {
    if (!this.master) return;
    var now = performance.now();
    if (!force && this._alignCooldownUntil && now < this._alignCooldownUntil) return;
    var mt = this.master.currentTime;
    var fixed = 0;
    this.audios.forEach(function (a) {
      if (a.el !== this.master && Math.abs(a.el.currentTime - mt) > (force ? 0.005 : 0.03)) {
        a.el.currentTime = mt;
        fixed += 1;
      }
    }, this);
    if (fixed) {
      this._realigns = (this._realigns || 0) + fixed; // 검증 배터리 계측용
      this._alignCooldownUntil = now + 1200; // seek 정착 대기 — 2초는 편차가 20ms 를 넘겨 놓침(실측)
    }
  };

  SyncPlayer.prototype.pause = function () {
    this.audios.forEach(function (a) { a.el.pause(); });
    this._stopLoop();
  };

  // 모던 엔진과 대칭 — pagehide/곡전환 시 오브젝트URL·오디오요소·메트로놈 컨텍스트 해제(누수 방지,
  // 코드리뷰 2026-07-14: 레거시엔 destroy 가 없어 shell.pagehide 가 아무것도 안 풀었음).
  SyncPlayer.prototype.destroy = function () {
    try { this.pause(); } catch (e) {}
    this.audios.forEach(function (a) {
      try { URL.revokeObjectURL(a.el.src); } catch (e) {}
      try { a.el.src = ''; } catch (e) {}
    });
    this.audios = [];
    try { if (this._metroCtx && this._metroCtx.state !== 'closed') this._metroCtx.close(); } catch (e) {}
    this._metroCtx = null;
  };

  SyncPlayer.prototype.seek = function (t) {
    this.audios.forEach(function (a) { a.el.currentTime = t; });
  };

  SyncPlayer.prototype.setRate = function (r) {
    this.rate = r;
    this.audios.forEach(function (a) {
      a.el.preservesPitch = true; // 피치 유지 배속 (REQ-PLAY-002)
      a.el.playbackRate = r;
    });
  };

  SyncPlayer.prototype.setStemVolume = function (name, v) {
    this.volumes[name] = v;
    this._applyAudible();
  };

  /* 솔로: 다중 토글(합집합 — 2026-07-11 사용자 실증 "드럼 솔로 누르면 베이스 풀림" 교정).
     다시 누르면 그 스템만 해제, 전부 해제되면 일반(음소거 규칙) 복귀. 음소거와 독립(솔로 우선). */
  SyncPlayer.prototype.setSolo = function (name) {
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

  SyncPlayer.prototype.setLoop = function (a, b) {
    this.loopA = a;
    this.loopB = b;
  };
  SyncPlayer.prototype.clearLoop = function () {
    this.loopA = this.loopB = null;
  };

  SyncPlayer.prototype._applyAudible = function () {
    var self = this;
    this.audios.forEach(function (a) {
      // 솔로 Set 이 비면 !muted, 있으면 포함 여부(합집합) — 뮤트와 독립·솔로 우선
      var audible = self.solos.length ? self.solos.indexOf(a.name) >= 0 : !self.muted[a.name];
      a.el.muted = !audible;
      a.el.volume = Math.max(0, Math.min(1, (self.volumes[a.name] || 0) * self.masterVol));
    });
  };

  SyncPlayer.prototype.setMaster = function (v) {
    this.masterVol = Math.max(0, Math.min(1, v));
    this._applyAudible();
  };

  /* ---- 메트로놈 — 곡의 실측 박 시각(동적 그리드)에 클릭을 예약(lookahead 스케줄러) ---- */
  // subEvery: 박 하나가 몇 클릭으로 세분됐는지(16분=4·셋잇단=3 등) — 소리 3단계(마디>박>세분) 구분용
  SyncPlayer.prototype.metroConfig = function (beatTimes, accentEvery, subEvery) {
    this._metroBeats = beatTimes && beatTimes.length ? beatTimes : null;
    this._metroAccent = accentEvery || 4;
    this._metroSubEvery = subEvery || 1;
    this._metroSched = {}; // 세분 변경 시 옛 예약 인덱스가 새 배열과 어긋나지 않게 초기화
  };
  SyncPlayer.prototype.setMetro = function (on) {
    this._metroOn = !!on;
    if (on && !this._metroCtx) {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (Ctx) this._metroCtx = new Ctx();
    }
    if (this._metroCtx && this._metroCtx.state === 'suspended') this._metroCtx.resume();
  };
  SyncPlayer.prototype.setMetroVol = function (v) {
    this._metroVol = Math.max(0, Math.min(1, v));
  };
  SyncPlayer.prototype._metroTick = function () {
    if (!this._metroOn || !this._metroBeats || !this._metroCtx || !this.isPlaying()) return;
    var now = this.currentTime();
    var horizon = now + 0.35 * this.rate; // 곡 시간 기준 예약 창(배속 반영)
    var beats = this._metroBeats;
    // 이진 탐색으로 now 이후 첫 박
    var lo = 0, hi = beats.length - 1;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (beats[mid] <= now) lo = mid + 1; else hi = mid; }
    for (var i = lo; i < beats.length && beats[i] <= horizon; i++) {
      if (this._metroSched[i]) continue;
      this._metroSched[i] = true;
      var dt = (beats[i] - now) / this.rate; // 실제 경과 시간 = 곡 시간 / 배속
      var t0 = this._metroCtx.currentTime + Math.max(0.01, dt);
      var osc = this._metroCtx.createOscillator();
      var g = this._metroCtx.createGain();
      // 3단계 소리: 마디 첫 클릭(높음) > 박 시작(중간) > 세분 클릭(낮음)
      var freq = 880;
      if (i % this._metroAccent === 0) freq = 1318;
      else if (this._metroSubEvery > 1 && i % this._metroSubEvery !== 0) freq = 660;
      osc.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, t0);
      // '전체 음량' 라벨 약속 지키기 — 클릭도 마스터를 따라간다(리뷰 확정: 음량 0 인데 클릭만 울림)
      g.gain.exponentialRampToValueAtTime(0.6 * this._metroVol * this.masterVol + 0.0001, t0 + 0.004);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.07);
      osc.connect(g);
      g.connect(this._metroCtx.destination);
      osc.onended = function () { try { osc.disconnect(); g.disconnect(); } catch (e) {} }; // 노드 누수 방지(활성 엔진과 동일, 코드검사 2026-07-17)
      osc.start(t0);
      osc.stop(t0 + 0.1);
    }
    // 오래된 예약 기록 정리(시크/루프 되감기 대응: 과거로 돌아오면 다시 예약 가능해야 함)
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
      // A-B 반복 (REQ-PLAY-004): 경계 도달 시 A 로 — 전 스템 함께
      if (self.loopA != null && self.loopB != null && t >= self.loopB) {
        self.seek(self.loopA);
        t = self.loopA;
      }
      if (self.onTick) self.onTick(t, self.duration());
      self._raf = requestAnimationFrame(frame);
    }
    this._raf = requestAnimationFrame(frame);
    // 드리프트 보험(2026-07-11 강화): 250ms 마다 20ms 초과 스템 재정렬 —
    // 50ms/1초는 이미 또렷한 플램으로 들리고 나서야 맞춰지는 체감이었음(사용자 실증)
    this._driftTimer = setInterval(function () {
      if (!self.isPlaying()) return;
      self._alignToMaster();
    }, 250);
    // 메트로놈 lookahead — 100ms 마다 다음 0.35초 창의 박 클릭 예약(단일 타이머 — 누수 방지)
    if (!this._metroTimer) {
      this._metroTimer = setInterval(function () { self._metroTick(); }, 100);
    }
  };

  SyncPlayer.prototype._stopLoop = function () {
    if (this._raf) cancelAnimationFrame(this._raf);
    if (this._driftTimer) clearInterval(this._driftTimer);
    if (this._metroTimer) clearInterval(this._metroTimer); // 재생/정지 반복 시 타이머 누적(리뷰 확정) 방지
    this._raf = this._driftTimer = this._metroTimer = null;
  };

  /* 카운트인 — 재생 전 예비박 클릭(첫 박 높은음). bpm 은 체감 박자(배속 반영은 호출부 몫) */
  SyncPlayer.prototype.countIn = function (bpm, beats, onDone) {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) { onDone(); return; }
    var ctx = new Ctx();
    var beat = 60 / (bpm || 100);
    var vol = 0.5 * this.masterVol + 0.0001; // 카운트인도 '전체 음량'을 따른다
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
    setTimeout(function () {
      ctx.close();
      onDone();
    }, Math.round(beats * beat * 1000));
  };

  SyncPlayer.prototype.isLegacy = true;
  window.SyncPlayerLegacy = SyncPlayer;
})();
