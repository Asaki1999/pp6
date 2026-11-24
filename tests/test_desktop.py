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
