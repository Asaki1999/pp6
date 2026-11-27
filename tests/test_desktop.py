"""桌面壳的文件选择器：过滤器字符串必须是 pywebview 能解析的格式。

pywebview 的 parse_file_type 只接受「描述 (*.ext;*.ext)」，且描述部分只能有
字母/数字/中文/空格——写成「Excel / PDF / Word (...」会直接抛
`is not a valid file filter`，前端表现为点了没反应或只弹一句 toast。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from pp6kit import desktop_api, instance
from pp6kit import winapp as winapp_mod
from tests._harness import case, skip


@case("DSK01", "desktop", "文件过滤器是 pywebview 合法格式（描述不能带斜杠）", "P0",
      ["Excel / zip / 多格式三组都合法", "多格式组同时含 pdf/docx"])
def _():
    try:
        from webview.util import parse_file_type
    except ImportError:
        skip("没装 pywebview：桌面文件过滤器检查跳过")
    groups = [desktop_api.EXCEL_TYPES, desktop_api.ZIP_TYPES, desktop_api.SOURCE_TYPES]
    for group in groups:
        for filt in group:
            desc, exts = parse_file_type(filt)
            assert desc, filt
            assert exts.startswith("*."), (filt, exts)
    desc, exts = parse_file_type(desktop_api.SOURCE_TYPES[0])
    assert "pdf" in exts and "docx" in exts, (desc, exts)
    assert "/" not in desc, desc


# ---------------------------------------------------------------- 单实例

@contextmanager
def _registry_tmp():
    """把 instance.json 指到临时目录，别动用户本机那份登记。"""
    tmp = Path(tempfile.mkdtemp(prefix="pp6-inst-"))
    old = os.environ.get(instance.REGISTRY_ENV)
    os.environ[instance.REGISTRY_ENV] = str(tmp / "instance.json")
    try:
        yield tmp / "instance.json"
    finally:
        if old is None:
            os.environ.pop(instance.REGISTRY_ENV, None)
        else:
            os.environ[instance.REGISTRY_ENV] = old
        shutil.rmtree(tmp, ignore_errors=True)


def _write_reg(path: Path, port: int, token: str, build: dict) -> None:
    path.write_text(json.dumps({
        "pid": os.getpid(), "port": port, "token": token,
        "url": f"http://127.0.0.1:{port}/",
        "frozen": build["frozen"], "version": build["version"], "commit": build["commit"],
    }, ensure_ascii=False), encoding="utf-8")


@contextmanager
def _fake_instance(port: int, build: dict, quit_ok: bool = True, holder=None):
    """假扮「正在跑的旧实例」：/api/version 报自己是谁，可选支持 /api/quit。"""
    hits: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):        # 别往测试输出里刷访问日志
            pass

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            hits.append(("GET", self.path))
            if self.path.startswith("/api/version"):
                self._send(200, {"ok": True, **build})
            else:
                self._send(404, {"ok": False})

        def do_POST(self):
            hits.append(("POST", self.path))
            if self.path == "/api/quit" and quit_ok:
                self._send(200, {"ok": True})
                # 模拟旧进程收到退出请求后「释放锁 + 关服务」
                threading.Timer(0.05, lambda: holder and holder.release()).start()
                threading.Timer(0.1, srv.shutdown).start()
            else:
                self._send(404, {"ok": False})

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield SimpleNamespace(port=port, hits=hits)
    finally:
        srv.shutdown()
        srv.server_close()


@case("DSK02", "desktop", "同一版本已在跑：只复用，不起第二个服务", "P0",
      ["探到同版本新版提交 → 复用它的地址", "不向旧实例发退出请求"])
def _():
    build = {"version": "test9.99", "commit": "abc1234", "frozen": False}
    with _registry_tmp() as reg:
        with _fake_instance(18975, build) as srv:
            holder = instance.InstanceLock(reg.with_suffix(".lock"))
            assert holder.acquire(0), "测试锁没拿到"
            try:
                _write_reg(reg, srv.port, "tok-dsk02", build)
                got = instance.claim(build, timeout=2)
                assert got.status == "reused", (got.status, got.detail)
                assert got.url == f"http://127.0.0.1:{srv.port}/", got.url
                assert not [h for h in srv.hits if h[1] == "/api/quit"], srv.hits
            finally:
                holder.release()


@case("DSK03", "desktop", "新版本启动时接管旧实例，并沿用同一个端口", "P0",
      ["版本/提交不同 → 发 /api/quit", "旧进程退出后拿到锁，端口可复用"])
def _():
    old = {"version": "test0.05", "commit": "old1111", "frozen": False}
    new = {"version": "test0.06", "commit": "new2222", "frozen": False}
    with _registry_tmp() as reg:
        holder = instance.InstanceLock(reg.with_suffix(".lock"))
        assert holder.acquire(0)
        try:
            with _fake_instance(18976, old, quit_ok=True, holder=holder) as srv:
                _write_reg(reg, srv.port, "tok-dsk03", old)
                got = instance.claim(new, timeout=4)
                assert got.status == "took-over", (got.status, got.detail)
                assert got.port == srv.port, (got.port, srv.port)
                assert ("POST", "/api/quit") in srv.hits, srv.hits
                assert not instance._port_open(srv.port), "旧端口还开着，新实例会顺延到 8771"
                assert got.lock is not None
                got.lock.release()
        finally:
            holder.release()


@case("DSK04", "desktop", "旧实例不响应退出请求时不误杀，明确报 blocked", "P0",
      ["老版本没有 /api/quit → 返回 blocked", "源码运行不按 PID 杀进程"])
def _():
    old = {"version": "test0.05", "commit": "old1111", "frozen": False}
    new = {"version": "test0.06", "commit": "new2222", "frozen": False}
    with _registry_tmp() as reg:
        holder = instance.InstanceLock(reg.with_suffix(".lock"))
        assert holder.acquire(0)
        try:
            with _fake_instance(18977, old, quit_ok=False) as srv:
                _write_reg(reg, srv.port, "tok-dsk04", old)
                got = instance.claim(new, timeout=1)
                assert got.status == "blocked", (got.status, got.detail)
                assert "没有退出" in got.detail, got.detail
                assert instance._port_open(srv.port), "旧实例不该被动手"
        finally:
            holder.release()


@case("DSK05", "desktop", "兜底结束进程只在冻结包生效，且先核对镜像名", "P0",
      ["源码运行 sys.executable 是 python，照杀会误伤", "pid 0/1/-1 一律拒绝"])
def _():
    assert instance.stop_process(os.getpid()) is False
    assert instance.stop_process(os.getpid(), sys.executable) is False
    assert instance.stop_process(0) is False
    assert instance.stop_process(-1) is False


@case("DSK06", "desktop", "冻结包与源码运行不算同一份；同提交重打也算新包", "P1",
      ["避免正式包复用到开发服务器", "同一提交但构建时间不同 → 视为新包并接管"])
def _():
    a = {"version": "test0.05", "commit": "abc1234", "frozen": True,
         "builtAt": "2026-11-24T09:00:00+08:00"}
    dev = {"version": "test0.05", "commit": "abc1234", "frozen": False,
           "builtAt": "2026-11-24T09:00:00+08:00"}
    assert instance.same_build(a, dev) is False
    assert instance.same_build(a, dict(a)) is True
    assert instance.same_build(a, {**a, "commit": "def5678"}) is False
    rebuilt = {**a, "builtAt": "2026-11-24T12:00:00+08:00"}
    assert instance.same_build(a, rebuilt) is False


# ---------------------------------------------------------------- Windows 应用窗口

@case("DSK07", "desktop", "Windows 应用窗口的命令行：--app + 独占 profile", "P0",
      ["带 --app=<url> / --user-data-dir / --window-size",
       "首启不弹欢迎与「设为默认浏览器」向导", "不带 --incognito"])
def _():
    from pp6kit import winapp

    args = winapp.launch_args(r"C:\Edge\msedge.exe", "http://127.0.0.1:8770/",
                              r"C:\Users\x\.pp6kit\appwindow")
    assert args[0].endswith("msedge.exe"), args[0]
    assert "--app=http://127.0.0.1:8770/" in args, args
    assert any(a.startswith("--user-data-dir=") for a in args), args
    assert "--window-size=1240,920" in args, args
    assert "--no-first-run" in args and "--no-default-browser-check" in args, args
    # 关掉最后一个窗口就让进程退出，否则 Edge 会「后台驻留」，生命周期判不准
    assert "--disable-background-mode" in args, args
    # profile 必须独占：共享默认 profile 时 Edge 会把窗口挂到已经在跑的进程上，
    # Popen 立刻返回，「窗口关没关」就再也判断不出来
    assert any("appwindow" in a for a in args if a.startswith("--user-data-dir=")), args
    joined = " ".join(args)
    assert "--incognito" not in joined, args


@case("DSK08", "desktop", "浏览器查找：Edge 优先、按候选顺序、找不到返回 None", "P0",
      ["同一台机器装了 Edge 与 Chrome → 选 Edge",
       "都没有 → None（调用方退回默认浏览器）", "变量没展开的候选直接跳过"])
def _():
    from pp6kit import winapp

    def only(*wanted):
        names = {w.replace("\\", "/").rsplit("/", 1)[-1].lower() for w in wanted}
        return lambda p: p.replace("\\", "/").rsplit("/", 1)[-1].lower() in names

    edge, chrome = r"C:\Edge\msedge.exe", r"C:\Chrome\chrome.exe"

    def fake_expand(p):
        for var, val in (("%ProgramFiles(x86)%", r"C:\PF86"),
                         ("%ProgramFiles%", r"C:\PF"),
                         ("%LocalAppData%", r"C:\Users\x\AppData\Local")):
            p = p.replace(var, val)
        return p

    got = winapp.find_browser(only(edge, chrome), expand=fake_expand)
    assert got and got[0] == "Edge", got
    got = winapp.find_browser(only(chrome), expand=fake_expand)
    assert got and got[0] == "Chrome", got
    assert winapp.find_browser(only(), expand=fake_expand) is None
    if os.name != "nt":
        # %ProgramFiles% 展开不了（非 Windows）的候选不能被当成存在的路径
        assert winapp.find_browser(lambda p: True) is None
    assert winapp.tray_available() in (True, False)


@case("DSK09", "desktop", "托盘菜单：打开主窗口 / 退出，工具提示不超长", "P1",
      ["菜单项有序且命令号唯一", "有「退出」，否则关窗后没法结束进程",
       "szTip 上限 127 字符"])
def _():
    from pp6kit import winapp

    items = winapp.tray_menu()
    labels = [label for _, label in items]
    ids = [cmd for cmd, _ in items]
    assert labels[0] == "打开主窗口", labels
    assert any("退出" in x for x in labels), labels
    assert len(set(ids)) == len(ids), ids
    assert all(isinstance(x, str) and x.strip() for x in labels), labels
    assert len(winapp.TRAY_TOOLTIP) <= 127, len(winapp.TRAY_TOOLTIP)


@case("DSK10", "desktop", "窗口关没关只看子进程：不会重复开窗，关了能重开", "P0",
      ["窗口开着时再次 open() 不启第二个进程", "进程退出后 is_open()=False 且能重开",
       "close() 会真的结束子进程"])
def _():
    from pp6kit import winapp

    class FakeProc:
        def __init__(self):
            self.alive = True
            self.terminated = False

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.terminated = True
            self.alive = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.alive = False

    spawned = []

    def spawn(args):
        proc = FakeProc()
        spawned.append((args, proc))
        return proc

    win = winapp.AppWindow(
        "http://127.0.0.1:8770/",
        browser=("Edge", Path(r"C:\Edge\msedge.exe")),
        profile=Path(tempfile.mkdtemp(prefix="pp6-win-")),
        spawn=spawn,
    )
    try:
        assert win.open() is True and len(spawned) == 1
        assert win.is_open() is True
        assert win.open() is True and len(spawned) == 1, "窗口还开着，不该再启一个"
        spawned[0][1].alive = False          # 用户关掉窗口
        assert win.is_open() is False
        assert win.open() is True and len(spawned) == 2, "窗口关了要能重开"
        win.close()
        assert spawned[-1][1].terminated is True
    finally:
        shutil.rmtree(win.profile, ignore_errors=True)


@case("DSK11", "desktop", "--app 模式下重复双击：先聚焦，聚焦不了也不报错", "P1",
      ["request_focus 走 /api/focus + 实例令牌", "对端没响应时返回 False 而不是抛异常"])
def _():
    assert instance.request_focus(0) is False
    with _registry_tmp() as reg:
        build = {"version": "test9.99", "commit": "abc1234", "frozen": False}
        _write_reg(reg, 18979, "tok-dsk11", build)
        # 端口上没人监听：要返回 False，不能抛异常把启动流程带崩
        assert instance.request_focus(18979, timeout=0.5) is False


# ---------------------------------------------------------- 应用窗口主循环

@contextmanager
def _patched(obj, **attrs):
    old = {key: getattr(obj, key) for key in attrs}
    for key, value in attrs.items():
        setattr(obj, key, value)
    try:
        yield
    finally:
        for key, value in old.items():
            setattr(obj, key, value)


def _drive_loop(port: int, make_window, make_tray, tray_ok: bool):
    """跑一遍 _run_app_window，返回 (rc, exits, 窗口实例)。

    Windows 的主循环是纯 Python：窗口、托盘、进程退出都可以换成假件，
    所以在 macOS 上也能验证「关窗 → 收摊 → 退出」与「托盘常驻」两条链路。
    """
    import types

    from pp6kit import app as desktop
    from pp6kit import webui

    exits: list[int] = []
    holder: dict = {}

    def wrap(url):
        holder["win"] = make_window(url)
        return holder["win"]

    fake_tray_mod = types.SimpleNamespace(Tray=lambda **kw: make_tray(**kw))
    had = "pp6kit.traywin" in sys.modules
    previous = sys.modules.get("pp6kit.traywin")
    sys.modules["pp6kit.traywin"] = fake_tray_mod
    try:
        with _patched(winapp_mod, AppWindow=wrap, tray_available=lambda: tray_ok), \
                _patched(desktop, _hard_exit=lambda code=0: exits.append(code),
                         WINDOW_SETTLE_SECONDS=0.2, WINDOW_POLL_SECONDS=0.05), \
                _patched(desktop.webbrowser, open=lambda url: False):
            httpd, url = webui.start_background(port)
            rc = desktop._run_app_window(url, httpd)
    finally:
        if had:
            sys.modules["pp6kit.traywin"] = previous
        else:
            sys.modules.pop("pp6kit.traywin", None)
    return rc, exits, holder.get("win")


@case("DSK12", "desktop", "没有托盘时关窗就退出：服务收摊、退出码 0", "P0",
      ["窗口关闭 → 主循环返回", "退出前关闭窗口与 HTTP 服务",
       "窗口只 open 一次（不会重复拉起）"])
def _():
    class Window:
        def __init__(self, url):
            self.url, self.opened, self.closed = url, 0, 0
            self.t0 = time.monotonic()

        @property
        def available(self):
            return True

        @property
        def name(self):
            return "Edge"

        def open(self):
            self.opened += 1
            return True

        def is_open(self):
            # 先存在 0.15 秒（窗口画出来）再关掉：模拟用户开了又关
            return time.monotonic() - self.t0 < 0.15

        def focus(self):
            return True

        def close(self, timeout=3.0):
            self.closed += 1

    rc, exits, win = _drive_loop(18981, Window, lambda **kw: None, tray_ok=False)
    assert rc == 0 and exits == [0], (rc, exits)
    assert win.opened == 1, win.opened
    assert win.closed >= 1, win.closed


@case("DSK13", "desktop", "有托盘：关窗不退出，托盘能重开窗口，退出才结束", "P0",
      ["窗口关掉后主循环继续等（服务常驻）", "托盘「打开」重新拉起窗口",
       "托盘「退出」→ 收摊并 os._exit(0)"])
def _():
    state = {"visible": True, "opened": 0, "closed": 0}

    class Window:
        def __init__(self, url):
            self.url = url

        @property
        def available(self):
            return True

        @property
        def name(self):
            return "Edge"

        def open(self):
            state["opened"] += 1
            state["visible"] = True
            return True

        def is_open(self):
            return state["visible"]

        def focus(self):
            return True

        def close(self, timeout=3.0):
            state["closed"] += 1
            state["visible"] = False

    class Tray:
        def __init__(self, on_open=None, on_quit=None, icon_path=""):
            self.on_open, self.on_quit, self.error = on_open, on_quit, ""
            self.stopped = False

        def start(self, timeout=4.0):
            # 0.15s 用户关窗 → 0.3s 点「打开主窗口」→ 0.6s 点「退出」
            threading.Timer(0.15, lambda: state.update(visible=False)).start()
            threading.Timer(0.3, lambda: self.on_open()).start()
            threading.Timer(0.6, lambda: self.on_quit()).start()
            return True

        def stop(self):
            self.stopped = True

    rc, exits, _win = _drive_loop(18982, Window, Tray, tray_ok=True)
    assert rc == 0 and exits == [0], (rc, exits)
    assert state["opened"] == 2, state       # 初始一次 + 托盘重开一次
    assert state["closed"] >= 1, state

