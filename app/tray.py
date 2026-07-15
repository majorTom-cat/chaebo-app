"""시스템 트레이 아이콘(Windows) — 순수 ctypes, 추가 의존성 0. 앱 창 모드에서 데몬 스레드로 실행.
좌클릭 = 앱 열기(표준 트레이 동작) · 우클릭 = 메뉴('chaebo 열기'·'설정'·'종료').
(사용자 요청 2026-07-13: 다른 앱들처럼 트레이에서 끄기·설정.)

원칙: 트레이는 부가기능 — 실패해도 앱 본체는 정상이어야 한다. 그래서 전 과정을 try/except 로 감싸고
데몬 스레드에서 자체 메시지 루프를 돌린다(win32 트레이는 만든 스레드에서 GetMessage 루프 필요).
콜백(on_open/on_settings/on_quit)은 run.py 가 스레드 안전하게 넘긴다.

★2026-07-13 교정(사용자 실기기 지적): ①좌클릭도 메뉴가 뜨던 것 → 좌클릭=열기/우클릭=메뉴 분리.
②우클릭 메뉴 글씨가 안 보이던 것 → ctypes restype/argtypes 미지정으로 64비트에서 핸들/포인터가
불안정(HMENU·HWND·LRESULT 절단)했던 것을 전 함수 시그니처 지정으로 교정. ③폴백 아이콘 상수를
주소로 잘못 넘기던 버그(MAKEINTRESOURCE) 교정.
주의: GUI/win32 라 무GUI 개발환경에서 미검증 — 실기기 확인 필요."""
import ctypes
import os
import threading
from ctypes import wintypes

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_NULL = 0x0000
NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
IDI_APPLICATION = 32512

ID_OPEN = 1001
ID_SETTINGS = 1002
ID_QUIT = 1003

# LRESULT 는 x64 에서 LONG_PTR(64bit) — c_long(32bit)로 두면 반환값이 잘려 기본 메시지 처리가
# 오동작할 수 있다. c_ssize_t 로 교정(win32 GUI 미검증 결함 방지, 2026-07-13).
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _setup_signatures(user32, shell32, kernel32):
    """전 Win32 호출의 restype/argtypes 지정 — 64비트에서 핸들(HMENU·HWND·HICON)·포인터·LRESULT 가
    기본 c_int 로 절단/부호확장되어 메뉴가 빈 채 뜨던 문제(사용자 지적) 근본 교정. c_void_p = 핸들."""
    from ctypes import c_void_p, c_int, c_uint, c_size_t, c_bool, POINTER
    W = wintypes
    kernel32.GetModuleHandleW.restype = c_void_p
    kernel32.GetModuleHandleW.argtypes = [W.LPCWSTR]
    user32.RegisterClassW.restype = W.ATOM
    user32.RegisterClassW.argtypes = [POINTER(WNDCLASS)]
    user32.CreateWindowExW.restype = c_void_p
    user32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                                       c_int, c_int, c_int, c_int, c_void_p, c_void_p, c_void_p, c_void_p]
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [c_void_p, c_uint, W.WPARAM, W.LPARAM]
    user32.LoadImageW.restype = c_void_p
    user32.LoadImageW.argtypes = [c_void_p, W.LPCWSTR, c_uint, c_int, c_int, c_uint]
    user32.LoadIconW.restype = c_void_p
    user32.LoadIconW.argtypes = [c_void_p, W.LPCWSTR]
    user32.CreatePopupMenu.restype = c_void_p
    user32.CreatePopupMenu.argtypes = []
    user32.AppendMenuW.restype = c_bool
    user32.AppendMenuW.argtypes = [c_void_p, c_uint, c_size_t, W.LPCWSTR]
    user32.TrackPopupMenu.restype = c_int
    user32.TrackPopupMenu.argtypes = [c_void_p, c_uint, c_int, c_int, c_int, c_void_p, c_void_p]
    user32.DestroyMenu.argtypes = [c_void_p]
    user32.GetCursorPos.argtypes = [POINTER(W.POINT)]
    user32.SetForegroundWindow.argtypes = [c_void_p]
    user32.PostMessageW.argtypes = [c_void_p, c_uint, W.WPARAM, W.LPARAM]
    user32.PostQuitMessage.argtypes = [c_int]
    user32.GetMessageW.argtypes = [POINTER(W.MSG), c_void_p, c_uint, c_uint]
    user32.TranslateMessage.argtypes = [POINTER(W.MSG)]
    user32.DispatchMessageW.argtypes = [POINTER(W.MSG)]
    shell32.Shell_NotifyIconW.restype = c_bool
    shell32.Shell_NotifyIconW.argtypes = [W.DWORD, POINTER(NOTIFYICONDATA)]


_ready = threading.Event()  # 트레이 아이콘이 실제로 떠 있는지 — run.py 가 X→숨김 안전 여부 판단에 씀


def is_running():
    """트레이 아이콘이 실제 생성돼 살아있으면 True. run.py 가 '창을 숨겨도 트레이로 되살릴 수 있는지'
    판단에 쓴다(트레이가 없는데 창을 숨기면 되살릴 길이 없어 갇힘 — 그 경우 X 는 정상 종료로 폴백)."""
    return _ready.is_set()


def start(icon_path, on_open=None, on_settings=None, on_quit=None):
    """데몬 스레드에서 트레이를 띄운다. 어떤 실패도 앱을 막지 않는다(부가기능)."""
    t = threading.Thread(target=_run, args=(icon_path, on_open, on_settings, on_quit),
                         daemon=True, name="chaebo-tray")
    t.start()
    return t


def _run(icon_path, on_open, on_settings, on_quit):
    try:
        _run_inner(icon_path, on_open, on_settings, on_quit)
    except Exception as e:  # noqa: BLE001 — 트레이 실패는 조용히(앱 본체 무영향)
        try:
            print(f"[tray] 비활성(무시): {e}", flush=True)
        except Exception:
            pass


def _run_inner(icon_path, on_open, on_settings, on_quit):
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    _setup_signatures(user32, shell32, kernel32)
    hinst = kernel32.GetModuleHandleW(None)

    def _safe(cb):
        try:
            if cb:
                cb()
        except Exception:  # noqa: BLE001
            pass

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            # 좌클릭 = 앱 열기(표준), 우클릭 = 메뉴. 예전엔 좌클릭도 메뉴를 띄웠다(사용자 지적).
            # 레거시 콜백: lparam 하위워드 = 마우스 메시지.
            low = lparam & 0xFFFF
            if low == WM_LBUTTONUP:
                _safe(on_open)
            elif low == WM_RBUTTONUP:
                _show_menu(user32, hwnd, on_open, on_settings, on_quit, shell32)
            return 0
        if msg == WM_COMMAND:
            cid = wparam & 0xFFFF
            if cid == ID_OPEN:
                _safe(on_open)
            elif cid == ID_SETTINGS:
                _safe(on_settings)
            elif cid == ID_QUIT:
                _remove_icon(shell32, hwnd)
                _safe(on_quit)
                os._exit(0)  # 확실한 종료(데몬 스레드에서 안전) — uvicorn 데몬도 함께 정리
            return 0
        if msg == WM_DESTROY:
            _remove_icon(shell32, hwnd)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wndproc)
    cls = WNDCLASS()
    cls.lpfnWndProc = proc
    cls.hInstance = hinst
    cls.lpszClassName = "chaeboTrayWndClass"
    user32.RegisterClassW(ctypes.byref(cls))  # 이미 등록됐어도 무해(재사용)

    hwnd = user32.CreateWindowExW(0, cls.lpszClassName, "chaebo", 0, 0, 0, 0, 0,
                                  None, None, hinst, None)
    if not hwnd:
        raise OSError("CreateWindow 실패")

    hicon = None
    if icon_path and os.path.isfile(icon_path):
        hicon = user32.LoadImageW(None, icon_path, IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
    if not hicon:
        # MAKEINTRESOURCE(IDI_APPLICATION): 정수 리소스 ID 를 포인터 값으로 넘긴다(예전엔 주소 32512 로
        # 잘못 넘겨 폴백이 깨졌음). 상위워드 0 이면 LoadIcon 이 리소스 ID 로 해석.
        hicon = user32.LoadIconW(None, ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wintypes.LPCWSTR))

    nid = NOTIFYICONDATA()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = hicon
    nid.szTip = "chaebo"
    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        raise OSError("Shell_NotifyIcon(ADD) 실패")
    _run._nid = nid  # GC 방지(구조체 수명 유지)
    _ready.set()  # 아이콘 실제 생성됨 — 이제 X→트레이 숨김이 안전(되살릴 수 있음)

    # 메시지 루프(이 스레드 전용)
    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        _ready.clear()  # 루프 종료 = 트레이 내려감


def _show_menu(user32, hwnd, on_open, on_settings, on_quit, shell32):
    """우클릭 메뉴. TPM_RETURNCMD 로 선택 결과를 직접 받아 처리 — WM_COMMAND 라우팅 의존을 줄여
    숨김 소유창 포커스 문제에도 동작을 견고하게 한다."""
    try:
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "chaebo 열기")
        user32.AppendMenuW(menu, MF_STRING, ID_SETTINGS, "설정")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "종료")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # 메뉴가 바로 사라지지 않게(win32 관례): 소유창을 앞으로 → TrackPopupMenu → 널 메시지.
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        # TPM_RETURNCMD: 선택 명령을 여기서 직접 처리(WM_COMMAND 로도 오지 않음)
        def _safe(cb):
            try:
                if cb:
                    cb()
            except Exception:  # noqa: BLE001
                pass
        if cmd == ID_OPEN:
            _safe(on_open)
        elif cmd == ID_SETTINGS:
            _safe(on_settings)
        elif cmd == ID_QUIT:
            _remove_icon(shell32, hwnd)
            _safe(on_quit)
            os._exit(0)
    except Exception:  # noqa: BLE001
        pass


def _remove_icon(shell32, hwnd):
    try:
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = hwnd
        nid.uID = 1
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    except Exception:  # noqa: BLE001
        pass
