"""单实例：一台机器只留一份在跑，新版启动时接管旧版。

为什么要有这个：Windows 上双击 exe 就是「再开一个进程」。旧进程没退出时仍占着
8770 端口、仍服务它那套静态文件；新下载的包只能顺延到 8771，浏览器里旧标签还
停在旧端口——用户看到的就是「新版没生效」，得先去任务管理器杀掉旧进程。

做法分三层：

1. 登记：``~/.pp6kit/instance.json`` 记录 pid / 端口 / 令牌 / 版本 / 提交 / 可执行
   文件。跨安装目录共享：新解压的文件夹也要能认出手上跑的是谁。
2. 锁：``instance.lock`` 上一把进程锁（POSIX ``flock`` / Windows ``msvcrt.locking``），
   保证「判断 + 接管」不会被两次连击插队。锁随句柄释放，进程崩溃也不会留下
   「请手动删锁文件」的坑。
3. 接管：新进程先探 ``/api/version``——

   - 同版本同提交 → 直接复用，自己退出，不重复起服务；
   - 版本或提交不同 → 请旧进程 ``POST /api/quit``（只认回环来源 + 启动时写进
     instance.json 的令牌），等端口释放后用**同一个端口**接管。浏览器里已经开着的
     标签刷新一下就是新版界面；
   - 旧进程没有这个接口（老版本）→ 退回按 PID 结束，但只在冻结包里、且先核对
     镜像名，绝不照着 ``sys.executable``（源码运行时是 python.exe）乱杀。

``--headless`` 与显式 ``--port`` 不参与这套机制：自检、CI、多开调试要能并存。
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REGISTRY_ENV = "PP6_INSTANCE_FILE"
DEFAULT_PORT = 8770
_IMAGE_NAME = "PP6ImportBuilder"
# 双击启动时没有控制台：tasklist/taskkill 若弹黑框，用户会以为程序在乱开窗口
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# ------------------------------------------------------------------ 登记表

def registry_path() -> Path:
    """实例登记表位置；测试用 ``PP6_INSTANCE_FILE`` 指到临时目录。"""
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pp6kit" / "instance.json"


def read_registry() -> dict:
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_registry(data: dict) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def clear_registry() -> None:
    """只删自己那份：接管那一刻，文件可能已经属于新进程了。"""
    data = read_registry()
    if data.get("pid") not in (None, os.getpid()):
        return
    try:
        registry_path().unlink()
    except FileNotFoundError:
        pass


def build_key() -> dict:
    """当前这份程序的标识：版本 + 提交 + 是否冻结。"""
    from . import version

    info = version.build_info()
    return {
        "version": info.get("version") or "",
        "commit": info.get("commit") or "",
        "builtAt": info.get("builtAt") or "",
        "frozen": bool(info.get("frozen")),
    }


def same_build(a: dict, b: dict) -> bool:
    """同一份程序吗？

    冻结包与源码运行永远算不同：否则开发时的 ``python -m pp6kit.app``
    会把正式包「复用」掉，用户看到的是开发服务器的界面。
    """
    if bool(a.get("frozen")) != bool(b.get("frozen")):
        return False
    ca, cb = (a.get("commit") or ""), (b.get("commit") or "")
    if ca and cb:
        if ca != cb:
            return False
        # 同一个提交也可能是两次构建（改了静态文件重打）：构建时间不同同样
        # 算不同版本，否则新包会被旧进程「复用」掉，界面上看还是旧的。
        ta, tb = (a.get("builtAt") or ""), (b.get("builtAt") or "")
        return not (ta and tb and ta != tb)
    va, vb = (a.get("version") or ""), (b.get("version") or "")
    return bool(va) and va == vb


# ------------------------------------------------------------------ 进程锁

class InstanceLock:
    """独占锁；进程活着才持有，退出（含崩溃）自动释放。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else registry_path().with_suffix(".lock")
        self._fh = None

    def acquire(self, timeout: float = 0.0) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        if fh.seek(0, os.SEEK_END) == 0:
            # Windows 的 msvcrt.locking 锁的是字节区间；空文件先补一个字节。
            fh.write(b"0")
            fh.flush()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if time.monotonic() >= deadline:
                    fh.close()
                    return False
                time.sleep(0.15)
                continue
            self._fh = fh
            return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()


# ------------------------------------------------------------------ 探测/退出

def _opener() -> urllib.request.OpenerDirector:
    # 环境里可能配了 HTTP_PROXY：本机请求走代理会莫名超时
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe(port: int | None, timeout: float = 1.0) -> dict | None:
    """问一下那个端口上是不是 PP6；是就返回它的版本信息。"""
    if not port:
        return None
    url = f"http://127.0.0.1:{int(port)}/api/version"
    try:
        with _opener().open(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    data["port"] = int(port)
    return data


def _post_local(port: int, token: str, path: str, timeout: float) -> bool:
    """向本机实例 POST 一个只认令牌的控制接口，返回它是否回 ok。"""
    if not port or not token:
        return False
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}{path}",
        data=b"{}",
        method="POST",
        headers={"X-PP6-Token": token, "Content-Type": "application/json"},
    )
    try:
        with _opener().open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:
        return False
    return bool(isinstance(data, dict) and data.get("ok"))


def request_quit(port: int, token: str, timeout: float = 2.5) -> bool:
    """请旧实例自己退出；老版本没有这个接口就返回 False。"""
    return _post_local(port, token, "/api/quit", timeout)


def request_focus(port: int, token: str = "", timeout: float = 2.0) -> bool:
    """请已有实例把窗口调到前面；窗口已经关了就让它在托盘里重开。

    第二次双击时窗口可能已经关掉（托盘还在），只「复用服务」用户会以为
    什么都没发生。令牌与 /api/quit 同一套：只认回环 + 实例令牌。
    """
    token = token or str(read_registry().get("token") or "")
    return _post_local(port, token, "/api/focus", timeout)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except OSError:
        return False


def wait_closed(port: int, timeout: float = 6.0) -> bool:
    if not port:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_open(port):
            return True
        time.sleep(0.15)
    return not _port_open(port)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def stop_process(pid: int, image: str = "") -> bool:
    """兜底：按 PID 结束旧实例。

    只对冻结包生效，并且先核对镜像名——源码运行时 ``sys.executable`` 是
    python.exe，照着它杀会连用户自己的脚本一起杀掉。
    """
    if pid <= 1 or pid == os.getpid() or not getattr(sys, "frozen", False):
        return False
    want = Path(image or sys.executable).name
    if not want:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=5,
                creationflags=_NO_WINDOW).stdout.decode("mbcs", "ignore")
        except Exception:
            return False
        if want.lower() not in out.lower():
            return False
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, timeout=10, creationflags=_NO_WINDOW)
        except Exception:
            return False
        return True

    try:
        comm = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                              capture_output=True, timeout=5).stdout.decode(
                                  "utf-8", "ignore").strip()
    except Exception:
        return False
    if not comm or Path(comm).name != want:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(20):
        if not _alive(pid):
            return True
        time.sleep(0.15)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ 启动决策

@dataclass
class Claim:
    """启动决策结果。

    status:
      - ``started``    没有别的实例，锁已在手，正常启动
      - ``took-over``  让旧实例退出了，锁已在手，接管它的端口
      - ``reused``     同版本已在跑，不必重复启动
      - ``blocked``    有实例在跑且退不掉，只能提示用户手动处理
    """

    status: str
    url: str = ""
    port: int = 0
    detail: str = ""
    lock: InstanceLock | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.status in ("started", "took-over")


def claim(build: dict | None = None, timeout: float = 6.0) -> Claim:
    """决定本次启动是开新实例、接管旧实例，还是直接复用。"""
    build = dict(build or build_key())
    lock = InstanceLock()

    if lock.acquire(0):
        return Claim("started", lock=lock)

    info = read_registry()
    live = probe(info.get("port"))
    if live is None:
        # 登记表过期（上次被强杀）：再等一次锁，可能是旧进程正在退。
        if lock.acquire(max(1.0, timeout / 2)):
            return Claim("started", lock=lock, detail="清理过期的实例登记")
        info = read_registry()
        live = probe(info.get("port"))
        if live is None:
            return Claim("blocked", detail="锁被占用，但那个端口上没有 PP6 在响应")

    port = int(live.get("port") or info.get("port") or 0)
    if same_build(live, build):
        return Claim("reused", port=port, url=f"http://127.0.0.1:{port}/",
                     detail=f"{live.get('version') or '?'} / {live.get('commit') or '?'}")

    old = f"{live.get('version') or '?'} / {live.get('commit') or '?'}"
    stopped = request_quit(port, str(info.get("token") or ""))
    if not stopped:
        stopped = stop_process(int(info.get("pid") or 0))
    if stopped and wait_closed(port, timeout) and lock.acquire(timeout):
        return Claim("took-over", port=port, detail=old, lock=lock)
    return Claim("blocked", port=port,
                 detail=f"旧实例 {old}（pid {info.get('pid') or '?'}）没有退出")


def register(port: int, token: str, build: dict | None = None) -> None:
    """把自己的存在写进登记表；进程退出时自动清掉自己那条。"""
    build = dict(build or build_key())
    write_registry({
        "pid": os.getpid(),
        "port": int(port),
        "url": f"http://127.0.0.1:{int(port)}/",
        "token": token,
        "path": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "version": build.get("version", ""),
        "commit": build.get("commit", ""),
        "builtAt": build.get("builtAt", ""),
        "startedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    atexit.register(clear_registry)
