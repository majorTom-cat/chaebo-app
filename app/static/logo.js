/* 로고 꾸미기 — 상단바 워드마크의 글씨체(data-font)·심볼(SVG)을 설정값대로 적용한다.
   모든 페이지(library·practice·settings)에서 로드. 저장은 서버(app_settings) + localStorage(무깜빡 즉시적용).
   설정 페이지는 window.chaeboLogo(fonts·symbols·apply·save)로 미리보기·저장을 한다. */
(function () {
  var GRAD = '<defs><linearGradient id="wmg" x1="4" y1="4" x2="28" y2="28" gradientUnits="userSpaceOnUse">' +
    '<stop stop-color="#f7bc6a"/><stop offset="1" stop-color="#ef6461"/></linearGradient></defs>';
  function svg(inner) { return '<svg viewBox="0 0 40 40" style="width:100%;height:100%;display:block">' + GRAD + inner + '</svg>'; }

  // 심볼 레지스트리(그림 로고) — 그림 로고 아티팩트에서 고른 것들. none = 심볼 없이 글자만.
  var SYMBOLS = {
    wave: svg('<g fill="url(#wmg)"><rect class="wm-bar" x="7" y="15" width="3.2" height="10" rx="1.6"/><rect class="wm-bar" x="13" y="10" width="3.2" height="20" rx="1.6"/><rect class="wm-bar" x="19" y="6" width="3.2" height="28" rx="1.6"/><rect class="wm-bar" x="25" y="11" width="3.2" height="18" rx="1.6"/><rect class="wm-bar" x="31" y="16" width="3.2" height="8" rx="1.6"/></g>'),
    note: svg('<g fill="url(#wmg)"><ellipse cx="13" cy="29" rx="6.4" ry="4.7" transform="rotate(-22 13 29)"/><rect x="17.8" y="8" width="2.9" height="21"/><path d="M20.7 8 c7 2 8.5 8.5 3.2 13.5 c3.6-6 1.4-10.5-3.2-13.5 z"/></g>'),
    beam: svg('<g fill="url(#wmg)"><ellipse cx="9" cy="30" rx="5.2" ry="3.9" transform="rotate(-20 9 30)"/><ellipse cx="26" cy="30" rx="5.2" ry="3.9" transform="rotate(-20 26 30)"/><rect x="12.9" y="12" width="2.5" height="18"/><rect x="29.9" y="9" width="2.5" height="21"/><path d="M12.9 12 L32.4 9 L32.4 13.4 L12.9 16.4 Z"/></g>'),
    hybrid: svg('<g fill="url(#wmg)"><ellipse cx="11" cy="29" rx="5.8" ry="4.3" transform="rotate(-20 11 29)"/><rect x="15.3" y="12" width="2.6" height="17"/><rect x="20" y="16" width="2.6" height="9" rx="1.3"/><rect x="24.4" y="11" width="2.6" height="14" rx="1.3"/><rect x="28.8" y="15" width="2.6" height="10" rx="1.3"/><rect x="33.2" y="18" width="2.6" height="7" rx="1.3"/></g>'),
    sound: svg('<g fill="url(#wmg)"><ellipse cx="12" cy="26" rx="5.6" ry="4.2" transform="rotate(-20 12 26)"/><rect x="16.1" y="10" width="2.6" height="16"/></g><g fill="none" stroke="url(#wmg)" stroke-width="2.6" stroke-linecap="round"><path d="M23 13 C 27 16 27 24 23 27"/><path d="M27 9 C 34 14 34 26 27 31"/></g>'),
    disc: svg('<circle cx="20" cy="20" r="15.5" fill="none" stroke="url(#wmg)" stroke-width="2.6"/><g fill="url(#wmg)"><ellipse cx="16" cy="25" rx="4.4" ry="3.3" transform="rotate(-20 16 25)"/><rect x="19.3" y="13" width="2.2" height="12"/><path d="M21.5 13 c5 1.4 6 6 2.4 9.6 c2.6-4.3 1-7.6-2.4-9.6 z"/></g>'),
    pick: svg('<path fill="url(#wmg)" d="M20 6 C 28 6 32 10 32 15.5 C 32 22.5 25.5 34 20 34 C 14.5 34 8 22.5 8 15.5 C 8 10 12 6 20 6 Z"/><g fill="#12151c" opacity="0.92"><ellipse cx="16.5" cy="24" rx="3.6" ry="2.7" transform="rotate(-20 16.5 24)"/><rect x="19.2" y="13" width="1.8" height="11"/><path d="M21 13 c4 1.1 4.8 5 1.9 7.8 c2-3.4 .7-6.1-1.9-7.8 z"/></g>'),
    clef: svg('<g fill="none" stroke="url(#wmg)" stroke-width="3.6" stroke-linecap="round"><path d="M14 12 C 24 11 30 17 26.5 24.5 C 24.5 29 18 31 13 26.5"/></g><g fill="url(#wmg)"><circle cx="14" cy="12.5" r="3.2"/><circle cx="31.5" cy="16" r="1.9"/><circle cx="31.5" cy="23" r="1.9"/></g>'),
    none: '',
  };
  var SYMBOL_LABELS = { wave: '파형', note: '음표', beam: '이어진 음표', hybrid: '음표+파형', sound: '울림', disc: '원형 뱃지', pick: '기타 픽', clef: '음자리표', none: '심볼 없음' };
  var FONTS = [
    { id: 'pacifico', label: 'Pacifico', family: "'Pacifico'" }, { id: 'lobster', label: 'Lobster', family: "'Lobster'" },
    { id: 'kaushan', label: 'Kaushan', family: "'KaushanScript'" }, { id: 'satisfy', label: 'Satisfy', family: "'Satisfy'" },
    { id: 'shrikhand', label: 'Shrikhand', family: "'Shrikhand'" }, { id: 'chewy', label: 'Chewy', family: "'Chewy'" },
    { id: 'monoton', label: 'Monoton', family: "'Monoton'" }, { id: 'bubbles', label: 'Bubbles', family: "'RubikBubbles'" },
    { id: 'default', label: '기본', family: "inherit" },
  ];
  var DEFAULT = { font: 'pacifico', symbol: 'wave' };

  function apply(font, symbol) {
    var wm = document.querySelector('.wordmark');
    if (!wm) return;
    wm.setAttribute('data-font', font || DEFAULT.font);
    var mark = wm.querySelector('.wordmark-mark');
    if (mark) mark.innerHTML = SYMBOLS[symbol] != null ? SYMBOLS[symbol] : SYMBOLS[DEFAULT.symbol];
  }

  function current() {
    try { return JSON.parse(localStorage.getItem('chaebo_logo')) || DEFAULT; } catch (e) { return DEFAULT; }
  }

  function save(font, symbol) {
    var v = { font: font, symbol: symbol };
    try { localStorage.setItem('chaebo_logo', JSON.stringify(v)); } catch (e) { /* 프라이빗 모드 등 — 무해 */ }
    apply(font, symbol);
    // 서버에도 저장(앱 재시작·다른 화면에도 유지)
    fetch('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ logo_font: font, logo_symbol: symbol }),
    }).catch(function () { /* 오프라인이어도 로컬 적용은 유지 */ });
  }

  // 즉시(로컬) 적용 — 깜빡임 없음
  var c = current();
  apply(c.font, c.symbol);
  // 서버값과 동기화(다른 기기·재시작 반영). 서버가 로컬과 다르면 서버 우선(단일 소스).
  fetch('/api/settings').then(function (r) { return r.json(); }).then(function (s) {
    if (!s) return;
    var f = s.logo_font || DEFAULT.font, sym = s.logo_symbol || DEFAULT.symbol;
    if (f !== c.font || sym !== c.symbol) {
      try { localStorage.setItem('chaebo_logo', JSON.stringify({ font: f, symbol: sym })); } catch (e) { }
      apply(f, sym);
    }
  }).catch(function () { });

  window.chaeboLogo = { SYMBOLS: SYMBOLS, SYMBOL_LABELS: SYMBOL_LABELS, FONTS: FONTS, apply: apply, save: save, current: current, DEFAULT: DEFAULT };
})();
