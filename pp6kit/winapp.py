"""Windows 应用窗口：借系统自带 Edge / Chrome 的 ``--app`` 模式。

为什么不继续用 pywebview
------------------------
pywebview 在 Windows 上靠 pythonnet 调 .NET 的 WebView2 封装，PyInstaller
冻结后这条链会卡在 CLR 初始化上（用户看到的是「未响应」），所以 2026-11
之前的版本干脆把 Windows 降级成「在浏览器里开一个页签」。页签关掉进程还在，
只能去任务管理器杀——这正是用户抱怨的那件事。

``--app`` 模式给的是另一个答案：没有地址栏、没有标签页，窗口就是一个独立
应用；配一份**独占的 user-data-dir**，窗口生命周期与一个 Edge 子进程严格
1:1，Python 侧用 ``Popen.poll()`` 就能准确知道「窗口关没关」，比任何猜测
窗口状态的办法都可靠。代价是窗口边框归浏览器、任务栏图标是 Edge 的，
换来的是零新增依赖、打包不用碰 .NET、关窗即退。

设计约束
--------
- 本模块非 Windows 也能导入（测试要在 macOS 上跑）：所有 ``ctypes.windll``
  都在函数体里取，模块顶层只做纯 Python 的事。
- 纯函数（``launch_args`` / ``find_browser``）与副作用分开，方便回归用例
  断言参数契约，不用真去开窗口。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

APP_NAME = "PP6 Studio"
WINDOW_SIZE = (1240, 920)

#: 托盘菜单 = (命令号, 文案)。放在这里而不是 traywin：本模块跨平台可导入，
#: 回归能在 macOS 上断言菜单结构；traywin 只负责把它画到 Windows 托盘上。
TRAY_MENU = ((1, "打开主窗口"), (2, "退出 PP6 Studio"))
TRAY_TOOLTIP = "PP6 Studio — 关窗口不退出，右键可退出"

# 按顺序尝试：Edge 是 Win10 1803+ 的系统组件，命中率最高；Chrome 兜底。
# 路径里的 %VAR% 交给 os.path.expandvars 展开（Windows 上认 %VAR%）。
BROWSER_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Edge", (
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
    )),
    ("Chrome", (
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    )),
)

# 页面标题（<title>PP6 Studio</title>）。找窗口、判存活都拿它当线索。
TITLE_HINT = APP_NAME

#: 进程起来了但窗口还没画出来的宽限期（Edge 首启新 profile 要一两秒）
WINDOW_GRACE = 8.0


def ptr_value(value) -> int:
    """把 ctypes 指针换成整数，并正确判断「空句柄」。

    坑：``c_void_p(0)`` 本身是**真值**，``if handle:`` 永远成立；而
    ``int(c_void_p(n))`` 会按 bytes 解析抛 ValueError。所有 Win32 句柄判断
    都必须走这里，否则「OpenProcess 失败」会被当成成功，后面拿空句柄操作。
    """
    if isinstance(value, int):
        return value
    return int(getattr(value, "value", 0) or 0)


def tray_available() -> bool:
    """本平台有没有托盘实现（只有 Windows 有）。"""
    return os.name == "nt"


def _expand_env(raw: str) -> str:
    """展开 ``%ProgramFiles%`` 这类 Windows 变量。

    非 Windows 上 ``os.path.expandvars`` 只认 ``$VAR``，会原样返回，
    于是「本机没装」和「路径没展开」两件事分不清。这里按 %VAR% 语法
    自己替换一遍：真实运行只发生在 Windows，非 Windows 的结果仅供回归
    验证候选顺序（含 % 的原样返回，由调用方跳过）。
    """
    if os.name == "nt":
        return os.path.expandvars(raw)
    import re

    def _sub(match):
        return os.environ.get(match.group(1), match.group(0))

    return re.sub(r"%([^%]+)%", _sub, raw)


def find_browser(exists=None, expand=None) -> tuple[str, Path] | None:
    """找可用的 Chromium 浏览器，返回 ``(名字, 可执行文件)``。

    ``exists`` / ``expand`` 可注入，回归用它模拟「装了 / 没装」两种机器，
    不用真去访问文件系统。
    """
    check = exists or (lambda p: Path(p).is_file())
    resolve = expand or _expand_env
    for name, paths in BROWSER_CANDIDATES:
        for raw in paths:
            candidate = resolve(raw)
            if "%" in candidate:      # 变量没展开成功（非 Windows 或变量缺失）
                continue
            if check(candidate):
                return name, Path(candidate)
    return None


def profile_dir() -> Path:
    """窗口独占的浏览器 profile。

    必须独立：共享用户的默认 profile 时，Edge 会把新窗口挂到已经跑着的
    Edge 进程上，我们 Popen 出来的进程立刻返回，窗口关没关就无从判断。
    目录也顺便让首页（新标签页/历史）与用户自己的浏览记录完全隔离。
    """
    override = os.environ.get("PP6_WINDOW_PROFILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pp6kit" / "appwindow"


def launch_args(exe, url: str, profile, size: tuple[int, int] = WINDOW_SIZE) -> list[str]:
    """拼 ``--app`` 窗口的命令行（纯函数，回归钉住这几条）。

    - ``--app=<url>``：无地址栏、无标签页；
    - ``--user-data-dir``：独占 profile（见上）；
    - ``--window-size``：首屏 1240x920，和 macOS 窗口一致；
    - ``--no-first-run`` / ``--no-default-browser-check`` /
      ``--disable-features=msEdgeFirstRunExperience``：新 profile 首启不要弹
      「欢迎使用 / 设为默认浏览器」，否则用户双击后先看到一堆向导；
    - ``--disable-sync``：别把本地页面同步进用户账号。
    """
    width, height = (int(size[0]), int(size[1]))
    return [
        str(exe),
        f"--app={url}",
        f"--user-data-dir={profile}",
        f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        # 关掉最后一个窗口就让进程退出：否则 Edge 会「后台驻留」，进程还在、
        # 窗口已经没了，生命周期就判断不准了
        "--disable-background-mode",
        "--disable-features=msEdgeFirstRunExperience,msEdgeSidebarV2",
    ]


class AppWindow:
    """一个 ``--app`` 窗口，生命周期由子进程句柄盯着。"""

    def __init__(self, url: str, browser=None, profile=None,
                 size: tuple[int, int] = WINDOW_SIZE, spawn=None):
        self.url = url
        self.browser = browser if browser is not None else find_browser()
        self.profile = Path(profile) if profile else profile_dir()
        self.size = size
        self._spawn = spawn or subprocess.Popen
        self._proc: subprocess.Popen | None = None
        self._started_at = 0.0

    # ------------------------------------------------------------ 状态
    @property
    def available(self) -> bool:
        return bool(self.browser)

    @property
    def name(self) -> str:
        return self.browser[0] if self.browser else ""

    def alive(self) -> bool:
        """子进程还活着吗（只看进程，不看窗口）。"""
        return self._proc is not None and self._proc.poll() is None

    def is_open(self) -> bool:
        """窗口还在吗？

        Windows 上以**窗口存在**为准：Edge 关掉最后一个窗口后进程可能还在
        后台驻留（后台模式/扩展），只看 ``poll()`` 会把「已关闭」永远判成
        「还开着」，没有托盘的机器就再也退不出去。进程活着但窗口还没画出来
        （刚启动那一两秒、或启动器把窗口交给子进程后自己退了）用宽限期兜。

        非 Windows 没有窗口枚举，退化成只看进程。
        """
        if _find_window() is not None:
            return True
        if not self.alive():
            return False
        return (time.monotonic() - self._started_at) < WINDOW_GRACE

    # ------------------------------------------------------------ 动作
    def open(self) -> bool:
        """打开窗口；已经开着就只把它提到前面。"""
        if self.alive() or self.is_open():
            self.focus()
            return True
        if not self.available:
            return False
        _, exe = self.browser  # type: ignore[misc]
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        args = launch_args(exe, self.url, self.profile, self.size)
        try:
            self._proc = self._spawn(args)
        except OSError:
            return False
        self._started_at = time.monotonic()
        return True

    def focus(self) -> bool:
        return _focus_window()

    def close(self, timeout: float = 3.0) -> None:
        """真正结束窗口进程（托盘「退出」/ 新实例接管时用）。"""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None

    def wait_closed(self, timeout: float | None = None) -> bool:
        """阻塞等窗口关闭（自检用，正常启动走轮询）。"""
        if self._proc is None:
            return True
        try:
            self._proc.wait(timeout=timeout)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------- 窗口枚举

def _browser_images() -> tuple[str, ...]:
    return ("msedge.exe", "chrome.exe")


_API: tuple | None = None


def _windows_api():
    """惰性拿 Windows API 句柄（第一次调用时绑好签名）；非 Windows 返回 None。

    为什么必须绑 argtypes：ctypes 默认把整数参数当 32 位 ``int`` 传，
    而 HWND / HANDLE 在 64 位下是 8 字节指针——不声明就会**截断高位**，
    枚举到的窗口句柄传到下一个 API 里就变成了别的（或无效）窗口，
    表现为「怎么都聚焦不了」「窗口存在性判断忽真忽假」。
    """
    global _API
    if _API is not None:
        return _API or None
    if os.name != "nt":
        _API = ()
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [ENUMPROC, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                    ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _API = (ctypes, wintypes, user32, kernel32, ENUMPROC)
        return _API
    except Exception:
        _API = ()
        return None


def _process_image(pid: int) -> str:
    """取进程可执行文件名（小写），失败返回空串。"""
    api = _windows_api()
    if api is None:
        return ""
    ctypes, wintypes, _user32, kernel32, _enumproc = api
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not ptr_value(handle):
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _find_window(title_hint: str = TITLE_HINT):
    """找一个标题含 ``title_hint`` 且属于 Edge/Chrome 的可见顶层窗口。"""
    api = _windows_api()
    if api is None:
        return None
    ctypes, wintypes, user32, _kernel32, WNDENUMPROC = api

    hits: list[int] = []

    def _callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if title_hint not in buf.value:
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if _process_image(pid.value) in _browser_images():
                hits.append(ptr_value(hwnd))
                return False
        except Exception:
            return True
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_callback), 0)
    except Exception:
        return None
    return hits[0] if hits else None


def _focus_window(title_hint: str = TITLE_HINT) -> bool:
    """把窗口恢复到前台。失败不报错——聚焦只是顺手的事。"""
    hwnd = _find_window(title_hint)
    if hwnd is None:
        return False
    api = _windows_api()
    if api is None:
        return False
    _ctypes, _wintypes, user32, _kernel32, _enumproc = api
    try:
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def tray_menu() -> list[tuple[int, str]]:
    """托盘菜单的纯数据（回归用）。"""
    return list(TRAY_MENU)


def executable_icon() -> str:
    """冻结包里的 app.ico（托盘与 exe 图标共用）；找不到返回空串。"""
    try:
        from . import resources

        path = resources.resource_root() / "assets" / "app.ico"
        return str(path) if path.is_file() else ""
    except Exception:
        return ""
