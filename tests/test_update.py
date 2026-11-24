"""版本指纹与自助升级。

这一组**不允许联网**：GitHub 只作为数据源被 mock 掉，回归才能离线跑。
真正连网的部分（TLS、代理、限流）在发布前用 `tools/regress.py --only UPD`
打不出结论，必须人工点一次「检查更新」验收。
"""
from __future__ import annotations

import hashlib
import http.server
import json
import threading

from pp6kit import updater, version
from tests._harness import case, skip


# ---------------------------------------------------------------- 版本比较

@case("UPD01", "update", "版本号按数字比较，不按字符串", "P0",
      ["test0.9 < test0.10", "v 前缀", "git describe 后缀"])
def _():
    assert version.compare_versions("test0.10", "test0.9") == 1
    assert version.compare_versions("test0.9", "test0.10") == -1
    assert version.compare_versions("test0.23", "test0.23") == 0
    assert version.compare_versions("v1.2.3", "1.2.3") == 0
    # git describe 的 -3-gabc 是「领先 3 个提交」，不是版本 3
    assert version.canonical("test0.23-3-gabc123") == "test0.23"
    assert version.compare_versions("test0.23-3-gabc", "test0.23") == 0
    # 单段八位日期不参与数字比较，避免 nightly 被当成天文数字的新版本
    assert version.compare_versions("nightly-20261124", "test0.23") is None
    assert version.compare_versions("", "test0.23") is None


@case("UPD02", "update", "日期版按构建时间判断，留出 CI 延迟容差", "P0",
      ["nightly 用时间戳", "90 秒容差"])
def _():
    newer, basis = version.is_newer(
        "nightly-20261124", "2026-11-24T10:00:00Z",
        "nightly-20261123", "2026-11-23T10:00:00Z")
    assert newer and basis == "time"
    # 同一次构建：CI 构建时间早于 Release 发布时间几十秒，不能算更新
    newer, _ = version.is_newer(
        "nightly-20261124", "2026-11-24T10:00:30Z",
        "nightly-20261124", "2026-11-24T10:00:00Z")
    assert not newer
    assert version.parse_time("2026-11-24T10:00:00+08:00") is not None
    assert version.parse_time("不是时间") is None


@case("UPD03", "update", "按平台挑安装包，挑不到不硬塞", "P0",
      ["macOS 附件", "Windows 附件", "缺失返回 None"])
def _():
    release = {"assets": [
        {"name": "PP6ImportBuilder-windows.zip", "size": 10, "browser_download_url": "w"},
        {"name": "PP6-Studio-macos.zip", "size": 20, "browser_download_url": "m"},
    ]}
    assert updater.pick_asset(release, "darwin")["url"] == "m"
    assert updater.pick_asset(release, "win32")["url"] == "w"
    # 名字不合规范时按关键字兜底
    odd = {"assets": [{"name": "PP6-macOS-build.zip", "size": 1,
                       "browser_download_url": "x"}]}
    assert updater.pick_asset(odd, "darwin")["url"] == "x"
    # 只有一个 zip 时可以兜底；两个都不匹配就不能猜
    assert updater.pick_asset({"assets": [{"name": "a.zip", "browser_download_url": "a"}]},
                              "darwin")["url"] == "a"
    assert updater.pick_asset({"assets": [
        {"name": "a.zip", "browser_download_url": "a"},
        {"name": "b.zip", "browser_download_url": "b"},
    ]}, "darwin") is None
    assert updater.pick_asset({"assets": []}, "win32") is None


# ---------------------------------------------------------------- 查更新（离线）

def _release(tag="test0.99", published="2026-11-24T00:00:00Z"):
    return {
        "tag_name": tag,
        "published_at": published,
        "html_url": f"https://example.invalid/tag/{tag}",
        "body": "本次更新：升级入口",
        "assets": [{
            "name": "PP6-Studio-macos.zip", "size": 123,
            "browser_download_url": "https://example.invalid/m.zip",
        }, {
            "name": "PP6ImportBuilder-windows.zip", "size": 456,
            "browser_download_url": "https://example.invalid/w.zip",
        }],
    }


@case("UPD04", "update", "检查更新有缓存，但结论随当前版本重算", "P0",
      ["24 小时缓存", "升级后不再提示", "不重复请求 GitHub"])
def _():
    calls = {"n": 0}

    def fake_fetch(_repo, timeout=10):
        calls["n"] += 1
        return _release()

    old_fetch, old_state = updater.fetch_latest, updater.STATE_FILE
    old_build = version.build_info
    tmp = _tmp_file("update-state.json")
    try:
        updater.fetch_latest = fake_fetch
        updater.STATE_FILE = tmp
        version.build_info = lambda: {"version": "test0.10", "commit": "abc",
                                      "builtAt": "2026-11-20T00:00:00+08:00",
                                      "channel": "release", "frozen": True,
                                      "platform": "darwin", "repo": "x/y"}
        first = updater.check(force=True)
        assert first["ok"] and first["hasUpdate"] is True, first
        assert first["latest"]["asset"]["name"] == "PP6-Studio-macos.zip"
        assert calls["n"] == 1

        # 第二次不强制：走缓存，不再请求
        second = updater.check()
        assert calls["n"] == 1 and second["cached"] is True

        # 模拟「已经升到最新」：缓存里还是旧 Release，但结论必须重算成无更新
        version.build_info = lambda: {"version": "test0.99", "commit": "def",
                                      "builtAt": "2026-11-24T00:00:00+08:00",
                                      "channel": "release", "frozen": True,
                                      "platform": "darwin", "repo": "x/y"}
        third = updater.check()
        assert third["hasUpdate"] is False, third
        assert third["current"]["version"] == "test0.99"
    finally:
        updater.fetch_latest, updater.STATE_FILE = old_fetch, old_state
        version.build_info = old_build
        tmp.unlink(missing_ok=True)


@case("UPD05", "update", "网络失败时给人话，不抛栈", "P1",
      ["连不上 GitHub", "检查结果 ok=false"])
def _():
    def boom(_repo, timeout=10):
        raise RuntimeError("连不上 GitHub，请检查网络或代理")

    old_fetch, old_state = updater.fetch_latest, updater.STATE_FILE
    tmp = _tmp_file("update-state-2.json")
    try:
        updater.fetch_latest = boom
        updater.STATE_FILE = tmp
        result = updater.check(force=True)
        assert result["ok"] is False
        assert "GitHub" in result["error"]
        assert result["hasUpdate"] is False
        assert result["current"]["version"]
    finally:
        updater.fetch_latest, updater.STATE_FILE = old_fetch, old_state
        tmp.unlink(missing_ok=True)


@case("UPD06", "update", "新版本没有本平台安装包时明确报错", "P1",
      ["只有 Windows 包", "不提示可升级"])
def _():
    release = _release()
    release["assets"] = [a for a in release["assets"] if "windows" in a["name"]]
    old_fetch, old_state = updater.fetch_latest, updater.STATE_FILE
    old_build = version.build_info
    tmp = _tmp_file("update-state-3.json")
    try:
        updater.fetch_latest = lambda _repo, timeout=10: release
        updater.STATE_FILE = tmp
        version.build_info = lambda: {"version": "test0.10", "commit": "",
                                      "builtAt": "2026-11-20T00:00:00+08:00",
                                      "channel": "release", "frozen": True,
                                      "platform": "darwin", "repo": "x/y"}
        result = updater.check(force=True, platform_key="darwin")
        assert result["ok"] is False
        assert result["hasUpdate"] is False
        assert "安装包" in result["error"]
    finally:
        updater.fetch_latest, updater.STATE_FILE = old_fetch, old_state
        version.build_info = old_build
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- 下载

class _ZipHandler(http.server.BaseHTTPRequestHandler):
    body = b""
    truncate = False

    def do_GET(self):
        size = len(self.body) - (100 if self.truncate else 0)
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        self.wfile.write(self.body[:size])

    def log_message(self, *args):
        pass


@case("UPD07", "update", "下载按大小校验，进度事件不止一条", "P0",
      ["sha256", "Content-Length 不符即失败", "下载目录隔离"])
def _():
    payload = b"PK" + b"\x00" * (300 * 1024)
    _ZipHandler.body = payload
    _ZipHandler.truncate = False
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ZipHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    old_dir = updater.DOWNLOAD_DIR
    tmp = _tmp_dir()
    seen: list[tuple[int, int]] = []
    try:
        updater.DOWNLOAD_DIR = tmp
        url = f"http://127.0.0.1:{httpd.server_address[1]}/pkg.zip"
        got = updater.download({"name": "PP6-Studio-macos.zip", "size": len(payload),
                                "url": url}, on_progress=lambda g, t: seen.append((g, t)))
        assert got["size"] == len(payload)
        assert got["sha256"] == hashlib.sha256(payload).hexdigest()
        assert len(seen) > 1, seen
        assert seen[-1][0] == len(payload)

        # 服务端少给 100 字节：必须报错，且不留下 .part
        _ZipHandler.truncate = True
        try:
            updater.download({"name": "PP6-Studio-macos.zip", "size": len(payload),
                              "url": url}, on_progress=None)
        except RuntimeError as exc:
            assert "不完整" in str(exc), exc
        else:
            raise AssertionError("少给的下载没有报错")
        assert not list(tmp.glob("*.part")), list(tmp.iterdir())
    finally:
        updater.DOWNLOAD_DIR = old_dir
        httpd.shutdown()
        for path in tmp.iterdir():
            path.unlink(missing_ok=True)
        tmp.rmdir()


@case("UPD08", "update", "源码运行不自我替换，只交出包路径", "P0",
      ["非冻结包走 manual", "不触碰安装目录"])
def _():
    if version.is_frozen():
        skip("当前就是冻结包，跳过源码态断言")
    path = _tmp_file("fake-update.zip")
    try:
        path.write_bytes(b"PK\x03\x04")
        result = updater.install(path, 8770)
        assert result["mode"] == "manual", result
        assert result["ok"] is False
        assert result["path"] == str(path)
    finally:
        path.unlink(missing_ok=True)


@case("UPD09", "update", "升级流的事件顺序可被前端消费", "P1",
      ["SSE 事件", "已是最新不下载", "error 事件收尾"])
def _():
    old_fetch, old_state = updater.fetch_latest, updater.STATE_FILE
    old_build = version.build_info
    tmp = _tmp_file("update-state-4.json")
    try:
        updater.STATE_FILE = tmp
        version.build_info = lambda: {"version": "test0.99", "commit": "",
                                      "builtAt": "2026-11-24T00:00:00+08:00",
                                      "channel": "release", "frozen": True,
                                      "platform": "darwin", "repo": "x/y"}
        updater.fetch_latest = lambda _repo, timeout=10: _release()
        events = list(updater.install_stream(8770))
        stages = [e["stage"] for e in events]
        assert stages[0] == "status", events
        assert stages[-1] == "done", events
        assert events[-1]["result"]["mode"] == "uptodate", events[-1]

        def boom(_repo, timeout=10):
            raise RuntimeError("连不上 GitHub，请检查网络或代理")

        updater.fetch_latest = boom
        events = list(updater.install_stream(8770))
        assert events[-1]["stage"] == "error", events
        assert "GitHub" in events[-1]["error"]
    finally:
        updater.fetch_latest, updater.STATE_FILE = old_fetch, old_state
        version.build_info = old_build
        tmp.unlink(missing_ok=True)


@case("UPD13", "update", "替换脚本等进程退出后换目录并清理", "P0",
      ["ditto 解压", "旧包搬走再落新包", "成功后删备份"])
def _():
    import os
    import shutil
    import subprocess
    import sys
    import tempfile
    import zipfile
    from pathlib import Path

    if sys.platform != "darwin":
        skip("替换脚本只在 macOS 上走 ditto")

    tmp = Path(tempfile.mkdtemp(prefix="pp6-mac-upd-"))
    bundle = tmp / "PP6 Studio.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "MacOS" / "PP6ImportBuilder").write_text("v1", encoding="utf-8")

    pack = tmp / "pack" / "PP6 Studio.app" / "Contents" / "MacOS"
    pack.mkdir(parents=True)
    (pack / "PP6ImportBuilder").write_text("v2", encoding="utf-8")
    zip_path = tmp / "pkg.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for item in (tmp / "pack" / "PP6 Studio.app").rglob("*"):
            zf.write(item, item.relative_to(tmp / "pack"))

    child = subprocess.Popen(["/bin/sleep", "1.2"])
    captured: dict = {}
    real_spawn, real_exe = updater._spawn, updater.sys.executable
    real_log = updater.LOG_FILE
    try:
        updater.LOG_FILE = tmp / "update.log"      # 别往用户真实日志里写测试记录
        updater.sys.executable = str(bundle / "Contents" / "MacOS" / "PP6ImportBuilder")
        updater._spawn = lambda script, args: captured.setdefault("args", args)
        result = updater._install_mac(zip_path, 8770, pid=child.pid)
        assert result["mode"] == "relaunch", result
        script = Path(captured["args"][1])
        assert "kill -0" in script.read_text(encoding="utf-8")

        # 用假的 open 跑脚本：只验证文件替换，不要在测试机上真弹一个窗口
        fakebin = tmp / "bin"
        fakebin.mkdir()
        stub = fakebin / "open"
        stub.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
        env = {**os.environ, "PATH": f"{fakebin}:/usr/bin:/bin"}
        runner = subprocess.Popen(["/bin/bash", str(script)], env=env)
        # 脚本在前台等旧进程退出；这里把子进程收尸（否则是僵尸，kill -0 永远成功）
        child.wait(timeout=10)
        try:
            runner.wait(timeout=20)
        finally:
            if runner.poll() is None:
                runner.kill()
    finally:
        updater._spawn, updater.sys.executable = real_spawn, real_exe
        updater.LOG_FILE = real_log
        if child.poll() is None:
            child.wait(timeout=10)

    try:
        assert (bundle / "Contents" / "MacOS" / "PP6ImportBuilder").read_text(encoding="utf-8") == "v2"
        assert not bundle.with_name(bundle.name + ".old").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 接口

@case("UPD10", "update", "版本接口返回真实构建指纹", "P0",
      ["/api/version", "frozen/platform", "不再是写死的 1.0.0"])
def _():
    from tests.test_webui import req
    status, _, body = req("/api/version")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["version"] and data["version"] != "1.0.0"
    assert data["platform"] == version.build_info()["platform"]
    assert isinstance(data["frozen"], bool)
    assert data["repo"].count("/") == 1


@case("UPD11", "update", "检查更新接口走同一套判定", "P1",
      ["/api/update/check?force=1", "hasUpdate", "附件名"])
def _():
    from tests.test_webui import req
    old_fetch, old_state = updater.fetch_latest, updater.STATE_FILE
    old_build = version.build_info
    tmp = _tmp_file("update-state-5.json")
    try:
        updater.STATE_FILE = tmp
        updater.fetch_latest = lambda _repo, timeout=10: _release()
        version.build_info = lambda: {"version": "test0.50", "commit": "",
                                      "builtAt": "2026-11-20T00:00:00+08:00",
                                      "channel": "release", "frozen": True,
                                      "platform": "darwin", "repo": "x/y"}
        status, _, body = req("/api/update/check?force=1")
        assert status == 200, body[:300]
        data = json.loads(body)
        assert data["ok"] is True
        assert data["hasUpdate"] is True
        assert data["latest"]["version"] == "test0.99"
        assert data["latest"]["asset"]["name"].endswith(".zip")
    finally:
        updater.fetch_latest, updater.STATE_FILE = old_fetch, old_state
        version.build_info = old_build
        tmp.unlink(missing_ok=True)


@case("UPD12", "update", "界面有升级入口且文案合规", "P1",
      ["关于页有检查更新", "确认后才升级", "脚本已引入"])
def _():
    from tests.test_webui import req
    _, _, html = req("/")
    page = html.decode("utf-8")
    for text in ("btnCheckUpdate", "btnDoUpdate", "btnConfirmUpdate", "updateState"):
        assert text in page, text
    assert "/static/update.js" in page
    assert "升级会关闭应用并重启" in page
    status, _, js = req("/static/update.js")
    assert status == 200
    assert b"/api/update/stream" in js


# ---------------------------------------------------------------- 工具

def _tmp_file(name: str):
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp(prefix="pp6-upd-")) / name


def _tmp_dir():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp(prefix="pp6-upd-dl-"))
