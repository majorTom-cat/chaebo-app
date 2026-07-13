"""시스템 트레이 아이콘(Windows) — 순수 ctypes, 추가 의존성 0. 앱 창 모드에서 데몬 스레드로 실행.
우클릭 메뉴: 'chaebo 열기'(창 앞으로) · '설정' · '종료'. (사용자 요청 2026-07-13: 다른 앱들처럼
트레이에서 끄기·설정.)

원칙: 트레이는 부가기능 — 실패해도 앱 본체는 정상이어야 한다. 그래서 전 과정을 try/except 로 감싸고
데몬 스레드에서 자체 메시지 루프를 돌린다(win32 트레이는 만든 스레드에서 GetMessage 루프 필요).
콜백(on_open/on_settings/on_quit)은 run.py 가 스레드 안전하게 넘긴다.

주의: GUI/win32 메시지루프 코드라 무GUI 개발환경에서 미검증 — 실기기 확인 필요."""
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
NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
TPM_RIGHTBUTTON = 0x0002
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800

ID_OPEN = 1001
ID_SETTINGS = 1002
ID_QUIT = 1003

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT,
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
    hinst = kernel32.GetModuleHandleW(None)

    def _safe(cb):
        try:
            if cb:
                cb()
        except Exception:  # noqa: BLE001
            pass

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam in (WM_RBUTTONUP, WM_LBUTTONUP):
                _show_menu(user32, hwnd)
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
    if not user32.RegisterClassW(ctypes.byref(cls)):
        # 이미 등록됐거나 실패 — 클래스 재사용 시도(무해)
        pass
    hwnd = user32.CreateWindowExW(0, cls.lpszClassName, "chaebo", 0, 0, 0, 0, 0,
                                  None, None, hinst, None)
    if not hwnd:
        raise OSError("CreateWindow 실패")

    hicon = None
    if icon_path and os.path.isfile(icon_path):
        hicon = user32.LoadImageW(None, icon_path, IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
    if not hicon:
        hicon = user32.LoadIconW(None, ctypes.c_wchar_p(32512))  # IDI_APPLICATION 폴백

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

    # 메시지 루프(이 스레드 전용)
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def _show_menu(user32, hwnd):
    try:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "chaebo 열기")
        user32.AppendMenuW(menu, MF_STRING, ID_SETTINGS, "설정")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "종료")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)  # 메뉴가 바로 사라지지 않게(win32 관례)
        user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)
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
