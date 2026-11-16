"""统一日志与崩溃兜底。

为什么必须有它
--------------
打包成窗口程序后（PyInstaller `console=False`），stdout / stderr 都是空设备：
`print` 不显示，异常只在内存里消失，原生崩溃连 Python 层都拦不住。
用户看到的只有「点了没反应」或「未响应」，而真正的原因一个字都拿不到。

所以这里做三件事：
1. 启动链路上每一步都写进 `~/.pp6kit/logs/app.log`，带相对毫秒，能看出卡在哪一段；
2. `sys.excepthook` / `threading.excepthook` 接管未捕获异常，落盘后在 Windows 上弹窗告知日志位置；
3. `faulthandler` 抓原生层崩溃（DLL 加载失败、段错误）到 `crash.log`。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path

_READY = False
_START = time.monotonic()
_crash_stream = None          # 必须留引用，否则 faulthandler 的 fd 被回收
_log = logging.getLogger("pp6kit.applog")

MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3


def log_dir() -> Path:
    d = Path.home() / ".pp6kit" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return log_dir() / "app.log"


def crash_path() -> Path:
    return log_dir() / "crash.log"


def elapsed() -> float:
    """从进程启动到现在的秒数，用于判断「卡在哪一步」。"""
    return time.monotonic() - _START


def get(name: str = "pp6kit") -> logging.Logger:
    return logging.getLogger(name)


def setup(level: int = logging.INFO) -> Path:
    """装好文件日志、崩溃捕获与异常兜底。重复调用只生效一次。"""
    global _READY, _crash_stream
    if _READY:
        return log_path()

    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        fh = logging.handlers.RotatingFileHandler(
            log_path(), maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass      # 日志目录不可写也不能让程序起不来

    # 窗口模式下 stderr 是 None 或不可用，加了会抛异常
    if sys.stderr is not None:
        try:
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(fmt)
            root.addHandler(sh)
        except Exception:
            pass

    # 原生崩溃：DLL 加载失败、栈溢出这类，Python 异常钩子拦不到
    try:
        import faulthandler
        _crash_stream = open(crash_path(), "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_crash_stream, all_threads=True)
    except Exception:
        _crash_stream = None

    sys.excepthook = _excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook

    _READY = True
    _log.info("日志开始记录 -> %s", log_path())
    return log_path()


def _excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    _log.critical("未捕获异常（主线程）\n%s", text)
    _alert("程序遇到错误", f"程序发生了未预期的错误，已记录到：\n{log_path()}\n\n{exc_type.__name__}: {exc}")
    sys.__excepthook__(exc_type, exc, tb)


def _thread_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    _log.critical("未捕获异常（线程 %s）\n%s",
                  getattr(args.thread, "name", "?"), text)


def _alert(title: str, text: str) -> None:
    """窗口模式没有控制台，只能靠系统弹窗把日志位置告诉用户。"""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)   # MB_ICONERROR
            return
        except Exception:
            pass
    # 非 Windows 或调用失败：终端里能看见，不额外弹窗
    if sys.stderr is not None:
        print(f"[{title}] {text}", file=sys.stderr, flush=True)


def fatal(message: str, exc: bool = True) -> None:
    """致命错误：落盘 + 告诉用户日志在哪，而不是让窗口静静地卡着。"""
    _log.critical(message, exc_info=exc)
    _alert("PP6 Studio 启动失败",
           f"{message}\n\n详细信息已写入：\n{log_path()}")


# 对外别名：调用方用它把「为什么没开窗口」直接告诉用户
alert = _alert


def dump_env(probe_gui: bool = False) -> None:
    """记录一次运行环境。排查「只有某台机器出问题」时，这段几乎必看。

    `probe_gui=False` 时**绝不 import webview / clr**：这两个包在 Windows 冻结包里
    会拉起 pythonnet 与 .NET 运行时，本身就要几秒，失败时还可能卡住主线程。
    环境信息不该成为启动的负担——要用窗口时才探（`probe_gui=True`）。
    """
    try:
        import pp6kit
        ver = getattr(pp6kit, "__version__", "?")
    except Exception:
        ver = "?"

    lines = [
        "-" * 60,
        f"PP6 Studio {ver}",
        f"Python {sys.version.split()[0]}  {platform.machine()}",
        f"系统 {platform.platform()}",
        f"可执行文件 {sys.executable}",
        f"冻结态 {getattr(sys, 'frozen', False)}  打包目录 {getattr(sys, '_MEIPASS', '')}",
        f"当前目录 {os.getcwd()}",
        f"配置文件 {Path.home() / '.pp6kit'}",
    ]

    # 只查是否存在，不真的导入——find_spec 不执行模块代码
    try:
        import importlib.util
        lines.append("pywebview " + ("已安装" if importlib.util.find_spec("webview") else "未安装"))
    except Exception as exc:
        lines.append(f"pywebview 无法判定：{exc}")

    if os.name == "nt":
        lines.append(f"WebView2 运行时：{_webview2_desc()}")
        if probe_gui:
            lines.append(f".NET / pythonnet：{_dotnet_desc()}")

    _log.info("运行环境\n" + "\n".join(lines))


WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_state() -> bool | None:
    """Windows 上查 WebView2 运行时。True=有，False=没有，None=不适用/查不到。

    pywebview 的 edgechromium 后端依赖这个组件。缺了它，窗口可能白屏、
    甚至卡在初始化上表现为「未响应」——所以先查清楚，别让它自己去撞。
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None

    keys = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
    ]
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as key:
                ver, _ = winreg.QueryValueEx(key, "pv")
                if ver:
                    return True
        except OSError:
            continue
    return False


def _webview2_desc() -> str:
    state = webview2_state()
    return {True: "已安装", False: "未检测到", None: "无法判断"}[state]


def _dotnet_desc() -> str:
    try:
        import clr  # noqa: F401
        return "pythonnet 可导入"
    except Exception as exc:
        return f"pythonnet 不可用（{exc.__class__.__name__}）"
