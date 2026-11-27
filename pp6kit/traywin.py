"""Windows 系统托盘（纯 ctypes 调 Shell_NotifyIconW，不引 pystray / pywin32）。

为什么自己写
------------
pystray 在 Windows 上依赖 pywin32，PyInstaller 要额外收集 COM 相关 dll；
我们只需要「一个图标 + 两项菜单」，用 Win32 原生接口 200 行就够，还不给
冻结包添新的失败点。

线程模型
--------
托盘必须在**有消息循环的线程**里创建，否则图标不显示（Explorer 靠窗口消息
投递点击）。所以 ``start()`` 起一个专用线程：注册窗口类 → 建隐藏窗口 →
``Shell_NotifyIconW(NIM_ADD)`` → ``GetMessage`` 循环，直到 ``stop()`` 发
``WM_CLOSE``。回调（打开/退出）在这个线程里执行，调用方只该往里投队列，
别在回调里做长活。

非 Windows 平台不导入本模块（app 侧按 ``os.name`` 判断），导入即说明环境
不对，直接抛 ImportError 更诚实。
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

if os.name != "nt":  # pragma: no cover - 由调用方保证
    raise ImportError("traywin 仅用于 Windows")

APP_NAME = "PP6 Studio"
CLS_NAME = CLASS_NAME = "PP6StudioTrayWindow"
WINDOW_TITLE = "PP6 Studio 托盘"

from .winapp import TRAY_MENU, TRAY_TOOLTIP, ptr_value  # noqa: E402

TOOLTIP = TRAY_TOOLTIP

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_NULL = 0x0000
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512
SW_RESTORE = 9

ID_OPEN = TRAY_MENU[0][0]
ID_QUIT = TRAY_MENU[-1][0]
#: 菜单项 = (命令号, 文案)。与 winapp.TRAY_MENU 同源，回归 DSK09 断言后者。
MENU_ITEMS = TRAY_MENU


# ------------------------------------------------------------------ 结构体

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    """Shell_NotifyIconW 的入参（Vista+ 完整版）。

    字段顺序与 winuser.h 一致；``uVersion`` 与 ``uTimeout`` 是 union，
    取联合体里较大的那个名字即可，位置一样。
    """
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
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", wintypes.DWORD),
        ("pt", POINT),
        ("lPrivate", wintypes.DWORD),
    ]


#: 回调签名：LRESULT WINAPI WndProc(HWND, UINT, WPARAM, LPARAM)。
#: 句柄统一走 c_ssize_t，避免 64 位下被默认的 c_int 截断。
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_ssize_t,
                             wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)


def menu_items() -> list[tuple[int, str]]:
    """托盘菜单的纯数据（回归用，不依赖 Windows）。"""
    return list(MENU_ITEMS)


class Tray:
    """一个托盘图标。

    ``on_open``：双击左键 / 菜单「打开主窗口」。
    ``on_quit``：菜单「退出」。
    两个回调都在托盘线程里跑，实现要足够短（投队列即可）。
    """

    def __init__(self, on_open=None, on_quit=None, tooltip: str = TOOLTIP,
                 icon_path: str = ""):
        self._on_open = on_open or (lambda: None)
        self._on_quit = on_quit or (lambda: None)
        self._tooltip = tooltip[:127]          # szTip 上限 128 含结尾 NUL
        self._icon_path = icon_path
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd: int = 0
        self._ok = False
        #: 失败原因（给日志用，不影响调用方决定是否降级）
        self.error = ""
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._wndproc_ref = None
        self._icon = None

    # ------------------------------------------------------------ 对外
    def start(self, timeout: float = 4.0) -> bool:
        if self._thread is not None:
            return self._ok
        self._thread = threading.Thread(target=self._run, name="pp6-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ok

    def stop(self) -> None:
        if self._hwnd:
            try:
                self._user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    # ------------------------------------------------------------ 内部
    def _bind_api(self) -> None:
        """显式声明签名：句柄是 64 位指针，ctypes 默认按 int 处理会截断。"""
        u, k = self._user32, self._kernel32
        u.RegisterClassExW.restype = wintypes.WORD
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     ctypes.c_size_t, ctypes.c_ssize_t]
        u.LoadIconW.restype = wintypes.HICON
        u.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCVOID]
        u.LoadImageW.restype = wintypes.HANDLE
        u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.CreatePopupMenu.restype = wintypes.HMENU
        u.AppendMenuW.restype = wintypes.BOOL
        u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                  ctypes.c_size_t, wintypes.LPCWSTR]
        u.TrackPopupMenu.restype = wintypes.UINT
        u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                     wintypes.LPVOID]
        u.DestroyMenu.argtypes = [wintypes.HMENU]
        u.GetMessageW.restype = ctypes.c_int
        u.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                  wintypes.UINT, wintypes.UINT]
        u.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        u.DispatchMessageW.restype = ctypes.c_ssize_t
        u.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   ctypes.c_size_t, ctypes.c_ssize_t]
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        u.Shell_NotifyIconW.restype = wintypes.BOOL
        u.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                        ctypes.POINTER(NOTIFYICONDATAW)]
        u.DestroyWindow.argtypes = [wintypes.HWND]
        k.GetModuleHandleW.restype = wintypes.HMODULE
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        # Shell_NotifyIcon 在 shell32，不在 user32。写错了不会报错，
        # 只会「图标永远不出现」，很难查——签名也一起绑上。
        s = ctypes.windll.shell32
        s.Shell_NotifyIconW.restype = wintypes.BOOL
        s.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                        ctypes.POINTER(NOTIFYICONDATAW)]

    def _load_icon(self):
        u = self._user32
        if self._icon_path:
            try:
                handle = u.LoadImageW(None, self._icon_path, IMAGE_ICON, 0, 0,
                                      LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if ptr_value(handle):
                    return handle
            except Exception:
                pass
        try:
            return u.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        except Exception:
            return None

    def _popup(self, hwnd: int) -> None:
        """右键菜单。SetForegroundWindow 是必须的，否则点别处菜单不消失。"""
        u = self._user32
        menu = u.CreatePopupMenu()
        if not ptr_value(menu):
            return
        picked = 0
        try:
            for cmd, label in MENU_ITEMS:
                if cmd == ID_QUIT:
                    # 退出项与上面隔开：误点「打开」代价小，误点退出代价大
                    u.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                u.AppendMenuW(menu, MF_STRING, cmd, label)
            point = POINT()
            u.GetCursorPos(ctypes.byref(point))
            u.SetForegroundWindow(hwnd)
            picked = u.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                point.x, point.y, 0, hwnd, None)
            u.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            u.DestroyMenu(menu)
        if picked == ID_OPEN:
            self._safe(self._on_open)
        elif picked == ID_QUIT:
            self._safe(self._on_quit)

    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except Exception:
            pass

    def _on_message(self, hwnd, message, wparam, lparam):
        if message == WM_TRAY:
            event = int(lparam) & 0xFFFF
            if event in (WM_LBUTTONDBLCLK, WM_LBUTTONUP):
                self._safe(self._on_open)
            elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._popup(hwnd)
            return 0
        if message == WM_CLOSE:
            self._user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self) -> None:
        try:
            self._bind_api()
            hinst = self._kernel32.GetModuleHandleW(None)
            self._wndproc_ref = WNDPROC(self._on_message)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = CLASS_NAME
            self._user32.RegisterClassExW(ctypes.byref(wc))
            # 注册失败通常是「已经注册过」（上一次托盘没退干净），窗口照样能建。
            hwnd = self._user32.CreateWindowExW(
                0, CLASS_NAME, WINDOW_TITLE, 0, 0, 0, 0, 0,
                None, None, hinst, None)
            if not ptr_value(hwnd):
                self._ready.set()
                return
            self._hwnd = ptr_value(hwnd)
            self._icon = self._load_icon()

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self._icon
            nid.szTip = self._tooltip
            # Shell_NotifyIcon 在 shell32，不在 user32 —— 写错会静默变成「没托盘」
            if not ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                self.error = "Shell_NotifyIcon 注册失败（Explorer 未运行？）"
                self._ready.set()
                return
            self._ok = True
            self._ready.set()

            while True:
                msg = MSG()
                got = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):
                    break
                self._user32.TranslateMessage(ctypes.byref(msg))
                self._user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self.error = self.error or f"{exc.__class__.__name__}: {exc}"
            self._ok = False
        finally:
            if self._hwnd:
                try:
                    nid = NOTIFYICONDATAW()
                    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                    nid.hWnd = self._hwnd
                    nid.uID = 1
                    ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                except Exception:
                    pass
            self._hwnd = 0
            self._ready.set()
