/* 업데이트 공용 로직(사용자 요청 2026-07-13) — 라이브러리의 새 버전 자동 안내와 설정의
   '업데이트 확인'이 같은 API·같은 자동 재시작 흐름을 쓰도록 한 곳에 모은다.
   (CLAUDE.md 11: 같은 동작은 한 구현으로 — 두 화면이 각자 조립하면 곧 갈린다.)

   흐름: check(최신 vs 현재 비교) → apply(바뀐 부분만 델타 교체) → restart(앱 자동 껐다 켜기). */
(function () {
  'use strict';
  function j(r) { return r.json(); }

  var U = {
    /* 공개 version.json 을 읽어 최신 버전과 현재 버전을 비교.
       반환: {enabled, current, latest, newer, url, notes} 또는 {enabled:false} / {error:true}. */
    check: function () {
      return fetch('/api/update-check').then(j);
    },

    /* 바뀐 부분(app/·run.py)만 델타로 받아 제자리 교체. 반환: {ok, version}. */
    apply: function () {
      return fetch('/api/apply-update', { method: 'POST' }).then(j);
    },

    /* 앱을 자동으로 껐다 켠다. 서버가 곧 종료되므로 응답을 못 받을 수도 있는데(정상),
       그때도 성공으로 간주한다 — 재시작 도우미가 새 앱을 띄운다. */
    restart: function () {
      return fetch('/api/restart', { method: 'POST' }).then(j)
        .catch(function () { return { ok: true }; });
    },

    /* 델타 적용 → (잠깐 안내) → 자동 재시작. 각 화면은 onStage(stage[, data]) 로 UI 를 갱신한다.
       stage: 'applying' | 'applied'(data=apply 응답) | 'apply-failed' | 'restarting'. */
    applyAndRestart: function (onStage) {
      onStage = onStage || function () {};
      onStage('applying');
      return U.apply().then(function (res) {
        if (!res || !res.ok) { onStage('apply-failed', res); return false; }
        onStage('applied', res);
        return new Promise(function (resolve) {
          setTimeout(function () {
            onStage('restarting');
            U.restart().then(function () { resolve(true); });
          }, 1400);  // '다 됐어요 — 자동으로 껐다 켜요' 를 읽을 시간
        });
      }).catch(function () { onStage('apply-failed'); return false; });
    }
  };

  window.chaeboUpdate = U;
})();
