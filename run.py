"""python run.py — 첫 실행 준비(엔진·모델·ffmpeg 자동 다운로드) → 서버 기동 → 화면 열기.
(REQ-OPS-005 — run.bat 이 이 파일을 실행한다.)

기본은 **chaebo 전용 앱 창**(pywebview + 윈도우 내장 WebView2). 앱 창을 못 쓰면(WebView2 없음·
pywebview 미설치·창 생성 실패) 자동으로 **웹브라우저 폴백**. 사용자가 직접 웹으로 열고 싶으면
`run.py web`(또는 환경변수 CHAEBO_BROWSER=1) — run.bat 이 인자를 그대로 넘긴다.
(로드맵 불변 원칙: 'A 항상 동작하는 폴백 유지'.)
"""
import os
import sys

# 어떤 실행 경로에서도 한글·특수문자(엠대시 등) 출력이 안전하게. 한국어 Windows 기본 stdout 은
# cp949 라 print 의 '—' 가 크래시하고, bootstrap 의 except 가 이를 '인터넷 실패'로 오표시했음(실측
# 2026-07-11). run.bat 은 PYTHONUTF8=1 을 걸지만, 다른 실행 경로도 안전하게 여기서 stdout 을 UTF-8 로.
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# 번들판은 pythonw.exe(콘솔 없음)로 실행 — 그땐 sys.stdout/stderr 가 None 이라 print 가 크래시한다.
# 로그 파일로 돌려 크래시 방지 + 문제 시 확인 가능하게.
if sys.stdout is None or sys.stderr is None:
    try:
        _logf = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "chaebo-log.txt"),
                     "a", encoding="utf-8", errors="replace")
        if sys.stdout is None:
            sys.stdout = _logf
        if sys.stderr is None:
            sys.stderr = _logf
    except Exception:
        pass
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 이 파일이 있는 폴더(앱 루트)를 경로에 추가 — 포터블(임베더블) 파이썬은 스크립트 폴더를
# 자동으로 sys.path 에 안 넣어(._pth 격리) 'import app' 이 실패하던 문제 교정(실측 2026-07-12).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import threading
import time
import webbrowser
from urllib.request import urlopen

_PORT = os.environ.get("PORT", "8765")
URL = "http://127.0.0.1:" + _PORT + "/"
HEALTH = "http://127.0.0.1:" + _PORT + "/api/health"


def _open_browser():
    webbrowser.open(URL)


# 열기 방식 저장 파일 — 설치 시 선택값을 여기 쓰고, 앱 설정에서 바꾸면 다시 쓴다. run.py 는
# 인자 없이 실행되면(바탕화면 'chaebo' 아이콘) 이 파일을 읽어 앱 창/웹브라우저를 정한다. 그래서
# 설치 후에도 앱 안에서 방식을 바꿀 수 있다(사용자 지적: 웹으로 설치하면 앱으로 못 켬 2026-07-12).
_MODE_FILE = os.path.join(_HERE, "open_mode.txt")


def _read_open_mode():
    try:
        with open(_MODE_FILE, encoding="utf-8") as f:
            return f.read().strip().lower()
    except Exception:
        return ""


def _want_browser():
    args = [a.lower() for a in sys.argv[1:]]
    if any(a in ("web", "--web", "browser", "--browser", "브라우저") for a in args):
        return True
    if any(a in ("app", "--app", "window", "--window", "앱") for a in args):
        return False  # 명시적 앱 창 요청(설정 저장값 무시)
    if os.environ.get("CHAEBO_BROWSER") == "1":
        return True
    return _read_open_mode() == "web"  # 저장된 방식(없으면 기본=앱 창)


# 단일 인스턴스: 이미 chaebo 가 떠 있는데 또 실행하면 중복 창·중복 서버(포트 충돌)가 생긴다
# (검사 배터리로 발견 2026-07-12). 명명 뮤텍스로 감지 — 둘째 인스턴스는 서버를 또 띄우지 않고
# 기존 것을 열어 보여준 뒤 종료한다. (뮤텍스는 프로세스 수명 동안 보유; 죽으면 OS 가 자동 해제)
_MUTEX_NAME = "chaebo-singleton-mutex-v1"
_MUTEX_HANDLE = None


def _is_another_instance_running():
    """이미 실행 중인 chaebo 가 있으면 True. 첫 인스턴스면 뮤텍스를 잡고 False.
    비윈도우/실패면 False(검사 건너뜀 — 기존 동작 유지)."""
    global _MUTEX_HANDLE
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.CreateMutexW(None, False, _MUTEX_NAME)
        err = k.GetLastError()
        if not h:
            return False
        if err == 183:  # ERROR_ALREADY_EXISTS — 다른 인스턴스가 이미 잡고 있음
            return True
        _MUTEX_HANDLE = h  # 첫 인스턴스: 프로세스 수명 동안 보유
        return False
    except Exception:
        return False


def _serve_in_thread():
    """백그라운드 스레드에서 uvicorn 기동 — 시그널 핸들러는 메인 스레드 전용이라 끈다."""
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")  # REQ-SEC-001: 기본 로컬 전용
    port = int(os.environ.get("PORT", "8765"))
    config = uvicorn.Config("app.main:app", host=host, port=port)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    server.run()


def _wait_health(timeout=40.0):
    """앱 창이 빈 페이지·연결오류를 보이지 않게 서버가 뜰 때까지 대기."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(HEALTH, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def _wait_server_down(timeout=25.0):
    """자동 재시작(업데이트 후)용 — 이전 인스턴스(서버)가 완전히 내려갈 때까지 대기.
    health 가 끊기면 이전 프로세스가 종료된 것(os._exit) → 포트·단일 인스턴스 뮤텍스가 풀린다.
    이걸 기다린 뒤 새로 떠야 '포트 사용 중'·'이미 실행 중' 충돌이 안 난다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(HEALTH, timeout=1) as r:
                r.read(1)  # 아직 떠 있음
        except Exception:
            return True  # 내려감(정상)
        time.sleep(0.3)
    return False  # 타임아웃 — 그래도 진행(아래 뮤텍스 재시도가 막아줌)


def _keepalive():
    """데몬 서버 스레드가 도는 동안 메인 프로세스를 붙잡아 둔다(브라우저 폴백용)."""
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass


# chaebo 창 아이콘(파이썬 기본 아이콘 대신) — 설치판·개발판 같은 상대경로. 없으면 조용히 생략.
_ICON = os.path.join(_HERE, "app", "static", "chaebo.ico")

# 준비 중 스플래시 — 창을 먼저 띄우고(콘솔 없이) 그 안에서 준비를 진행한다.
# 로딩바·퍼센트는 cbProgress(pct,msg) 로 갱신(run.py 가 준비 단계마다 evaluate_js 로 호출).
_SPLASH_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#0f1115;color:#e8eaf0;
font-family:'Malgun Gothic','Segoe UI',sans-serif}
.wrap{height:100%;display:flex;flex-direction:column;align-items:center;
justify-content:center;gap:18px;padding:24px;text-align:center}
h1{color:#f0a848;font-size:40px;margin:0;letter-spacing:.5px}
.lead{color:#e8eaf0;margin:0;font-size:16px;line-height:1.7}
.sub{color:#8a91a0;margin:0;font-size:13px;line-height:1.7}
.barwrap{width:min(420px,80vw);margin-top:6px}
.bar{height:10px;border-radius:999px;background:#20242e;overflow:hidden}
.fill{height:100%;width:6%;border-radius:999px;
background:linear-gradient(90deg,#f0a848,#ffca7a);
transition:width .45s cubic-bezier(.4,0,.2,1)}
.meta{display:flex;justify-content:space-between;margin-top:8px;
font-size:12px;color:#8a91a0}
#msg{color:#b6bcc8}
.indet .fill{width:35%;animation:slide 1.3s ease-in-out infinite}
@keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
</style></head>
<body><div class="wrap">
<h1>chaebo</h1>
<p class="lead">연습할 준비를 하고 있어요.<br>처음 한 번만 잠깐 기다려 주세요.</p>
<div class="barwrap indet" id="barwrap">
  <div class="bar"><div class="fill" id="fill"></div></div>
  <div class="meta"><span id="msg">준비물을 확인하는 중이에요</span><span id="pct"></span></div>
</div>
<p class="sub">분리 엔진과 AI 모델을 인터넷에서 받아요 · 내 컴퓨터 안에서만 동작해요</p>
</div>
<script>
window.cbProgress=function(pct,msg){
  var f=document.getElementById('fill'),p=document.getElementById('pct'),
      m=document.getElementById('msg'),w=document.getElementById('barwrap');
  if(!f)return;
  if(pct>0){w.classList.remove('indet');f.style.width=Math.max(3,Math.min(100,pct))+'%';
            p.textContent=Math.round(pct)+'%';}
  if(msg)m.textContent=msg;
};
</script>
</body></html>"""


def _prepare_backend(report=None):
    """모델/엔진/ffmpeg 준비 후 서버 기동. 성공 시 True. report(pct,msg) 는 스플래시 로딩바."""
    from app import bootstrap
    bootstrap.ensure_all(progress=report)
    if report:
        report(96, "연습 화면을 켜는 중이에요")
    t = threading.Thread(target=_serve_in_thread, daemon=True)
    t.start()
    ok = _wait_health(180)
    if report and ok:
        report(100, "다 됐어요")
    return ok


def _run_app_window():
    """chaebo 전용 창을 먼저 띄우고(스플래시), 그 안에서 준비→앱 로드. 콘솔 없이 동작."""
    try:
        import webview
    except ImportError as e:
        # 앱 창 모드인데 pywebview 미설치 → 브라우저 폴백. '앱으로 설정했는데 웹으로 열림'의 한 원인이라
        # 로그로 남긴다(사용자 지적 2026-07-14: 웹→앱 전환 안 됨). pythonw 는 stdout 이 chaebo-log.txt.
        print(f"[open-mode] 앱 창 폴백→브라우저: pywebview 임포트 실패({e}). "
              f"설정은 '앱'이어도 이 PC 는 앱 창을 못 써 웹으로 엽니다.", flush=True)
        if _prepare_backend():
            _open_browser()
        _keepalive()
        return
    try:
        window = webview.create_window("chaebo", html=_SPLASH_HTML,
                                       width=1280, height=860, min_size=(900, 600))
    except Exception as e:
        # 창 생성 자체가 안 되면(WebView2 런타임 부재 등) 브라우저 폴백 — 마찬가지로 로그로 남긴다.
        print(f"[open-mode] 앱 창 폴백→브라우저: 창 생성 실패({e}). WebView2 런타임이 없을 수 있어요. "
              f"설정은 '앱'이어도 이 PC 는 앱 창을 못 써 웹으로 엽니다.", flush=True)
        if _prepare_backend():
            _open_browser()
        _keepalive()
        return

    def _work(win):
        import json

        def report(pct, msg):
            # evaluate_js 는 GUI 스레드로 안전 디스패치 — 준비 단계마다 스플래시 바 갱신.
            try:
                win.evaluate_js("window.cbProgress&&cbProgress(%d,%s)"
                                % (int(pct), json.dumps(msg, ensure_ascii=True)))
            except Exception:
                pass

        try:
            ok = _prepare_backend(report)
            if ok:
                win.load_url(URL)
            else:
                win.load_html("<body style='background:#0f1115;color:#e8eaf0;font-family:sans-serif;"
                              "text-align:center;padding-top:20vh'><h2>서버를 시작하지 못했어요</h2>"
                              "<p>인터넷 연결을 확인하고 다시 실행해 주세요.</p></body>")
        except Exception:
            win.load_html("<body style='background:#0f1115;color:#e8eaf0;font-family:sans-serif;"
                          "text-align:center;padding-top:20vh'><h2>준비 중 문제가 있었어요</h2>"
                          "<p>인터넷 연결을 확인하고 다시 실행해 주세요.</p></body>")

    # 최소화하면 트레이로 숨기고(백그라운드 유지), 닫기(X)=완전 종료 — 사용자 선택 2026-07-13
    # ('몰래 백그라운드 상주' 우려 반영: 상주는 사용자가 최소화할 때만). 트레이는 부가기능이라
    # 전 과정을 try/except 로 감싸 실패해도 앱 본체는 정상. GUI 코드라 실기기 확인 필요.
    # 최소화 = 평범하게 작업표시줄로(창을 숨기지 않는다). 예전엔 최소화 시 window.hide() 로 트레이에
    # 숨겨 작업표시줄에서 사라졌는데, 사용자에겐 '창이 꺼진 것처럼' 보였다(실기기 지적 2026-07-13:
    # 최소화 버튼이 창을 없애는 놀라운 효과). 트레이 아이콘은 편의 접근용으로 유지(좌클릭 열기·우클릭
    # 메뉴). 닫기(X)=완전 종료. '몰래 백그라운드 상주' 없음 — 창이 있어야 앱이 돈다.

    def _tray_open():
        try:
            window.show()
        except Exception:
            pass
        try:
            window.restore()
        except Exception:
            pass

    def _tray_settings():
        _tray_open()
        try:
            window.load_url(URL + "settings")
        except Exception:
            pass

    try:
        from app import tray as _tray
        _tray.start(_ICON if os.path.isfile(_ICON) else None,
                    on_open=_tray_open, on_settings=_tray_settings,
                    on_quit=lambda: os._exit(0))
    except Exception:
        pass  # 트레이 실패는 무시 — 앱은 정상 동작

    _icon = _ICON if os.path.isfile(_ICON) else None
    webview.start(_work, window, icon=_icon)  # 창 닫힐 때까지 블록


if __name__ == "__main__":
    # 자동 재시작(업데이트 적용 후 — 사용자 요청 2026-07-13): 이전 인스턴스가 띄운 '재시작 도우미'.
    # 이전 서버가 완전히 내려간 뒤에야 새로 뜬다(포트·단일 인스턴스 충돌 방지). 열기 방식(web/app)
    # 인자는 --relaunch 만 걷어내고 그대로 이어받는다. 창이 닫혔다가 새 창(또는 새 탭)으로 다시 열린다.
    _relaunch = "--relaunch" in sys.argv
    if _relaunch:
        sys.argv = [a for a in sys.argv if a != "--relaunch"]
        _wait_server_down()
        time.sleep(1.0)  # 포트/뮤텍스 해제 여유(윈도우 소켓 정리)
        # 이전 인스턴스가 뮤텍스를 놓을 때까지 짧게 재시도 — 보통 즉시 성공(그럼 뮤텍스 보유하고 진행).
        for _ in range(20):
            if not _is_another_instance_running():
                break
            time.sleep(0.4)
    elif _is_another_instance_running():
        # 이미 실행 중 — 중복 창/서버를 만들지 않고 기존 것을 브라우저로 열어 보여준 뒤 종료.
        try:
            _open_browser()
        except Exception:
            pass
        sys.exit(0)
    if _want_browser():
        if _prepare_backend():
            _open_browser()
        _keepalive()
    else:
        _run_app_window()
