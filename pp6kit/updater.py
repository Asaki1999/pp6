"""自助升级：查最新版本、下载、替换、重启。

设计取舍（都是为了「同事点一下就能用上新版」）：

- **只查 GitHub 公共 API**，匿名额度 60 次/小时；结果缓存到
  `~/.pp6kit/update.json`，自动检查 24 小时内不重复，手动检查也可先用缓存，
  避免整个办公室共用一个出口 IP 把额度打光。
- **先解压校验、再动正在用的目录**：确认新包里 `Contents/MacOS` / `_internal`
  结构完整才替换；替换失败自动回滚到旧版。
- **替换由独立脚本完成**：正在运行的 `.app` / `exe` 不能自己覆盖自己，
  所以先把「等待进程退出 → 换目录 → 重新打开」写成脚本交给系统执行。
- **非冻结包（源码运行）不自我替换**：开发时不至于把自己删了，只把包下好并提示路径。
- **任何一步失败都降级成手动**：把下载好的包和安装位置告诉用户，不让人卡死。
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import version

CONFIG_DIR = Path.home() / ".pp6kit"
STATE_FILE = CONFIG_DIR / "update.json"
DOWNLOAD_DIR = CONFIG_DIR / "updates"
LOG_FILE = CONFIG_DIR / "logs" / "update.log"

CACHE_TTL = 30 * 60                 # 手动检查：30 分钟内复用结果
AUTO_TTL = 24 * 60 * 60             # 启动自动检查：一天一次
MAX_BYTES = 300 * 1024 * 1024
TIMEOUT = 10

# 附件名与 workflow 里 upload/压缩的名字一一对应（改名要两处一起改）
ASSETS = {
    "darwin": "PP6-Studio-macos.zip",
    "win32": "PP6ImportBuilder-windows.zip",
}

_INSTALL_LOCK = threading.Lock()
_INSTALLING = {"on": False}


# ---------------------------------------------------------------- 小工具

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _ssl_context() -> ssl.SSLContext | None:
    """冻结包里未必带系统根证书，优先用随包的 certifi。"""
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return None


def _user_agent() -> str:
    return "PP6-Studio/" + str(version.build_info().get("version") or "unknown")


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 查最新版

def _curl() -> str | None:
    """系统 curl。macOS 自带，Windows 10 1803+ 自带；
    它走系统代理与系统根证书，是 Python TLS 被中间设备拦住时的活路。"""
    return shutil.which("curl") or (shutil.which("curl.exe") if os.name == "nt" else None)


def _curl_json(url: str, timeout: float) -> dict | None:
    exe = _curl()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-fsSL", "--max-time", str(int(timeout)),
             "-H", "Accept: application/vnd.github+json",
             "-H", f"User-Agent: {_user_agent()}", url],
            capture_output=True, timeout=timeout + 5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        raise RuntimeError("连不上 GitHub，请检查网络或代理")
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except ValueError:
        raise RuntimeError("GitHub 返回的内容无法解析") from None


def fetch_latest(repo: str, timeout: float = TIMEOUT) -> dict:
    """GitHub `releases/latest`。

    注意：`latest` 会跳过 prerelease，所以 main 分支的 `nightly` 不算；
    `test*` / `v*` tag 在 workflow 里是正常 Release，能查到。
    """
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": _user_agent(),
        "X-GitHub-Api-Version": "2022-11-28",
    })
    transport_error = ""
    try:
        kwargs = {"timeout": timeout}
        context = _ssl_context()
        if context is not None:
            kwargs["context"] = context
        with urllib.request.urlopen(request, **kwargs) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError("仓库还没有发布过版本") from None
        if exc.code in (403, 429):
            raise RuntimeError("GitHub 限流，请稍后再试") from None
        transport_error = f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        transport_error = str(getattr(exc, "reason", exc))
    except (TimeoutError, ssl.SSLError, OSError) as exc:
        transport_error = str(exc)
    except ValueError:
        raise RuntimeError("GitHub 返回的内容无法解析") from None

    # Python 的 TLS 栈在部分公司网络/旧系统上会被握手拦截，curl 往往还能通
    try:
        fallback = _curl_json(url, timeout)
    except RuntimeError:
        raise
    if fallback is not None:
        _log_tail(f"urllib 失败（{transport_error}），已用 curl 取到版本信息")
        return fallback
    raise RuntimeError("连不上 GitHub，请检查网络或代理")


def pick_asset(release: dict, platform_key: str | None = None) -> dict | None:
    """按平台挑附件；名字不匹配时按关键字兜底。"""
    assets = release.get("assets") or []
    wanted = ASSETS.get(platform_key or sys.platform)
    for asset in assets:
        if wanted and asset.get("name") == wanted:
            return _asset_row(asset)
    keyword = "macos" if (platform_key or sys.platform) == "darwin" else "windows"
    other = "windows" if keyword == "macos" else "macos"
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith(".zip") and keyword in name:
            return _asset_row(asset)
    zips = [a for a in assets if str(a.get("name") or "").lower().endswith(".zip")]
    # 唯一的 zip 且明显是另一个平台的：不能拿来充当本平台安装包
    if any(other in str(a.get("name") or "").lower() for a in zips):
        return None
    return _asset_row(zips[0]) if len(zips) == 1 else None


def _asset_row(asset: dict) -> dict:
    return {
        "name": asset.get("name") or "",
        "size": int(asset.get("size") or 0),
        "url": asset.get("browser_download_url") or asset.get("url") or "",
    }


def _payload(release: dict, info: dict, cached: bool = False,
             platform_key: str | None = None) -> dict:
    tag = str(release.get("tag_name") or "")
    published = str(release.get("published_at") or release.get("created_at") or "")
    newer, basis = version.is_newer(tag, published, info.get("version", ""),
                                   info.get("builtAt", ""))
    # 平台以调用方 / 构建指纹为准：测试会在 Linux 上模拟 darwin 的构建，
    # 真机两者本来就一致；直接看 sys.platform 会让「模拟平台」失效。
    asset = pick_asset(release, platform_key or info.get("platform"))
    missing = bool(newer and not asset)
    notes = str(release.get("body") or "")
    if len(notes) > 6000:
        notes = notes[:6000].rstrip() + "\n…"
    return {
        "ok": not missing,
        "current": {
            "version": info.get("version"),
            "commit": info.get("commit"),
            "builtAt": info.get("builtAt"),
            "channel": info.get("channel"),
            "frozen": info.get("frozen"),
        },
        "latest": {
            "version": tag,
            "publishedAt": published,
            "url": release.get("html_url") or "",
            "notes": notes,
            "asset": asset,
        },
        "hasUpdate": bool(newer and asset),
        "basis": basis,
        "checkedAt": _now_iso(),
        "cached": cached,
        "error": "这个版本没有当前系统的安装包" if missing else "",
    }


def check(force: bool = False, platform_key: str | None = None) -> dict:
    """查更新。`force=True` 用于手动点「检查更新」，一定走网络。

    缓存只存 Release 原始数据，不存算好的结论：升级后当前版本变了，
    结论必须重算，否则「刚升完还提示有新版本」。
    """
    info = version.build_info()
    state = _load_state()
    age = time.time() - float(state.get("at") or 0)
    if not force:
        release = state.get("release")
        if release and age < AUTO_TTL:
            return _payload(release, info, cached=True, platform_key=platform_key)
    try:
        release = fetch_latest(info["repo"])
    except RuntimeError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "current": info,
            "hasUpdate": False,
            "checkedAt": _now_iso(),
        }
    payload = _payload(release, info, platform_key=platform_key)
    _save_state({"at": time.time(), "release": release})
    return payload


# ---------------------------------------------------------------- 下载

def _clean_name(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name) or "pp6-update.zip"


def _download_urllib(url: str, part: Path, on_progress) -> tuple[int, int, str]:
    total = 0
    got = 0
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={
        "User-Agent": _user_agent(),
        "Accept": "application/octet-stream",
    })
    kwargs = {"timeout": 30}
    context = _ssl_context()
    if context is not None:
        kwargs["context"] = context
    with urllib.request.urlopen(request, **kwargs) as resp, open(part, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            got += len(chunk)
            if got > MAX_BYTES:
                raise RuntimeError("更新包超过 300MB，已中止")
            fh.write(chunk)
            digest.update(chunk)
            if on_progress:
                on_progress(got, total)
    return got, total, digest.hexdigest()


def _download_curl(url: str, part: Path, expect: int, on_progress) -> tuple[int, int, str]:
    """curl 下载 + 轮询文件大小报进度。

    为什么不用 `--progress-bar`：它往 stderr 刷 `\\r` 帧，跨平台解析容易出错；
    盯文件大小既稳，又能复用同一套「已下载 / 总大小」事件。
    """
    exe = _curl()
    if not exe:
        raise RuntimeError("连不上 GitHub，请检查网络或代理")
    proc = subprocess.Popen(
        [exe, "-fL", "--retry", "2", "--connect-timeout", "20", "--max-time", "900",
         "-o", str(part), url],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.time() + 920
    try:
        while proc.poll() is None:
            if time.time() > deadline:
                proc.kill()
                raise RuntimeError("下载超时，请稍后再试")
            got = part.stat().st_size if part.exists() else 0
            if on_progress:
                on_progress(got, expect)
            time.sleep(0.3)
    finally:
        stderr = proc.stderr.read().decode("utf-8", "ignore") if proc.stderr else ""
        if proc.stderr:
            proc.stderr.close()
    if proc.returncode != 0:
        _log_tail(f"curl 下载失败（{proc.returncode}）: {stderr.strip()[:300]}")
        raise RuntimeError("下载失败，请重试")
    got = part.stat().st_size if part.exists() else 0
    if got > MAX_BYTES:
        raise RuntimeError("更新包超过 300MB，已中止")
    digest = hashlib.sha256()
    with open(part, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return got, got, digest.hexdigest()


def download(asset: dict, on_progress=None) -> dict:
    """流式下载到 `~/.pp6kit/updates/`，边下边算 sha256。

    先用 urllib；TLS 被中间设备打断时退回 curl（同样走 HTTPS，不降级明文）。
    """
    url = asset.get("url") or ""
    if not url:
        raise RuntimeError("更新包地址为空")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOAD_DIR / _clean_name(asset.get("name") or "pp6-update.zip")
    part = dest.with_name(dest.name + ".part")
    expect = int(asset.get("size") or 0)
    try:
        try:
            got, total, sha = _download_urllib(url, part, on_progress)
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            _log_tail(f"urllib 下载失败（{exc}），改用 curl")
            got, total, sha = _download_curl(url, part, expect, on_progress)
        if (total and got != total) or (expect and got != expect):
            raise RuntimeError("下载不完整，请重试")
        os.replace(part, dest)
    except Exception:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    return {"path": str(dest), "size": got, "sha256": sha}


# ---------------------------------------------------------------- 替换

def _mac_bundle() -> Path | None:
    """从解释器路径反推 `.app`：.../PP6 Studio.app/Contents/MacOS/PP6ImportBuilder"""
    exe = Path(sys.executable).resolve()
    if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
        return exe.parents[2]
    return None


def _log_tail(text: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except Exception:
        pass


def _reveal(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif os.name == "nt":
            subprocess.Popen(["explorer", f"/select,{path}"])
    except Exception:
        pass


def _manual(zip_path: Path, message: str) -> dict:
    _reveal(zip_path)
    return {"ok": False, "mode": "manual", "message": message, "path": str(zip_path)}


def _validate_zip(zip_path: Path, root_name: str) -> zipfile.ZipFile:
    if not zip_path.is_file():
        raise RuntimeError("更新包不存在，请重新下载")
    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError("更新包损坏，请重新下载")
    archive = zipfile.ZipFile(zip_path)
    names = archive.namelist()
    if not any(name.startswith(root_name + "/") for name in names):
        archive.close()
        raise RuntimeError("更新包结构不对，已保留当前版本")
    return archive


def _spawn(script_path: Path, args: list[str]) -> None:
    """脱离当前进程执行替换脚本——脚本要等我们退出后才能动文件。"""
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


def _install_mac(zip_path: Path, port: int, pid: int | None = None) -> dict:
    bundle = _mac_bundle()
    if bundle is None:
        return _manual(zip_path, "没找到应用位置，请手动替换")
    if "/AppTranslocation/" in str(bundle):
        return _manual(zip_path, "应用在临时目录运行，请先拖进「应用程序」")
    parent = bundle.parent
    if not os.access(parent, os.W_OK):
        return _manual(zip_path, f"没有写入权限，请把应用移到「应用程序」后重试")

    staging = Path(tempfile.mkdtemp(prefix="pp6-update-"))
    archive = _validate_zip(zip_path, bundle.name)
    try:
        archive.close()
        result = subprocess.run(["ditto", "-x", "-k", str(zip_path), str(staging)],
                                capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError("解压失败，已保留当前版本")
        new_bundle = staging / bundle.name
        binary = new_bundle / "Contents" / "MacOS" / "PP6ImportBuilder"
        if not binary.is_file():
            raise RuntimeError("新包里缺少可执行文件，已保留当前版本")

        backup = bundle.with_name(bundle.name + ".old")
        script = staging / "install.command"
        script.write_text(f"""#!/bin/bash
exec >>{_q(LOG_FILE)} 2>&1
echo "开始替换 {bundle}"
while kill -0 {pid or os.getpid()} 2>/dev/null; do sleep 0.3; done
rm -rf {_q(backup)}
mv {_q(bundle)} {_q(backup)} || exit 1
if mv {_q(new_bundle)} {_q(bundle)}; then
  xattr -dr com.apple.quarantine {_q(bundle)} 2>/dev/null
  open {_q(bundle)}
  rm -rf {_q(backup)}
  echo "替换完成"
else
  mv {_q(backup)} {_q(bundle)}
  open {_q(bundle)}
  echo "替换失败，已回滚"
  exit 1
fi
rm -rf {_q(staging)}
""", encoding="utf-8")
        script.chmod(0o755)
        _spawn(script, ["/bin/bash", str(script)])
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        _log_tail(f"macOS 替换失败: {exc}")
        return _manual(zip_path, f"{exc}")
    _log_tail(f"macOS 准备替换: {bundle}")
    return {"ok": True, "mode": "relaunch", "message": "正在替换并重启，请稍候",
            "path": str(zip_path)}


def _install_windows(zip_path: Path, port: int, pid: int | None = None) -> dict:
    exe = Path(sys.executable)
    target = exe.parent
    staging = Path(tempfile.mkdtemp(prefix="pp6-update-"))
    try:
        archive = _validate_zip(zip_path, "PP6ImportBuilder")
        archive.extractall(staging)
        archive.close()
        new_dir = staging / "PP6ImportBuilder"
        new_exe = new_dir / "PP6ImportBuilder.exe"
        if not new_exe.is_file():
            raise RuntimeError("新包里缺少可执行文件，已保留当前版本")
        if not os.access(target, os.W_OK):
            raise RuntimeError("没有写入权限，请换到可写的目录再试")

        script = staging / "install.cmd"
        script.write_text(
            "@echo off\r\n"
            "setlocal\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {pid or os.getpid()}" 2>nul | find "{pid or os.getpid()}" >nul\r\n'
            "if not errorlevel 1 (\r\n"
            "  timeout /t 1 /nobreak >nul\r\n"
            "  goto wait\r\n"
            ")\r\n"
            f'robocopy "{new_dir}" "{target}" /E /IS /IT /R:2 /W:1 /NFL /NDL /NJH /NJS >nul\r\n'
            f'start "" "{target / exe.name}" --port {port}\r\n'
            "endlocal\r\n"
            'del "%~f0"\r\n',
            encoding="utf-8")
        _spawn(script, ["cmd.exe", "/c", str(script)])
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        _log_tail(f"Windows 替换失败: {exc}")
        return _manual(zip_path, str(exc))
    _log_tail(f"Windows 准备替换: {target}")
    return {"ok": True, "mode": "relaunch", "message": "正在替换并重启，请稍候",
            "path": str(zip_path)}


def _q(value) -> str:
    """shell 单引号转义；macOS 路径里的空格与中文都必须安全。"""
    text = str(value).replace("'", "'\\''")
    return f"'{text}'"


def install(zip_path: Path | str, port: int = 8770, pid: int | None = None) -> dict:
    """把下载好的包换成当前安装，返回 mode=relaunch / manual。"""
    path = Path(zip_path)
    if not version.is_frozen():
        return {"ok": False, "mode": "manual", "path": str(path),
                "message": "源码运行不自动替换，更新包已下载"}
    if sys.platform == "darwin":
        return _install_mac(path, port, pid)
    if os.name == "nt":
        return _install_windows(path, port, pid)
    return _manual(path, "当前系统不支持自动替换，请手动安装")


# ---------------------------------------------------------------- 一条龙

def install_stream(port: int = 8770):
    """SSE 生成器：查版本 → 下载（带进度）→ 替换。

    每个事件都是一行 JSON。任何异常都转成 error 事件，不往外抛——
    响应头已经发出去了，再抛只会让前端看到半截连接。
    """
    with _INSTALL_LOCK:
        if _INSTALLING["on"]:
            yield {"stage": "error", "error": "正在升级，请稍候"}
            return
        _INSTALLING["on"] = True
    try:
        yield {"stage": "status", "text": "正在获取最新版本"}
        result = check(force=True)
        if not result.get("ok"):
            yield {"stage": "error", "error": result.get("error") or "检查更新失败"}
            return
        latest = result.get("latest") or {}
        asset = latest.get("asset") or {}
        if not result.get("hasUpdate"):
            yield {"stage": "done", "result": {"ok": True, "mode": "uptodate",
                                               "message": "已经是最新版本"}}
            return
        if not asset.get("url"):
            yield {"stage": "error", "error": result.get("error") or "没有可用的安装包"}
            return

        yield {"stage": "status", "text": f"正在下载 {latest.get('version') or ''}",
               "version": latest.get("version"), "total": asset.get("size") or 0}
        last = [0.0]
        pending: queue.Queue = queue.Queue()

        def on_progress(got: int, total: int) -> None:
            now = time.time()
            if now - last[0] < 0.25 and got != total:
                return
            last[0] = now
            pending.put((got, total))

        # 下载在后台线程跑，主线程边取进度边发事件
        holder: dict = {}

        def work() -> None:
            try:
                holder["ok"] = download(asset, on_progress)
            except Exception as exc:
                holder["error"] = str(exc)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(0.25)
            while not pending.empty():
                got, total = pending.get()
                yield {"stage": "progress", "got": got, "total": total}
        while not pending.empty():
            got, total = pending.get()
            yield {"stage": "progress", "got": got, "total": total}
        if holder.get("error"):
            yield {"stage": "error", "error": holder["error"]}
            return

        downloaded = holder["ok"]
        yield {"stage": "status", "text": "下载完成，正在校验"}
        result_install = install(Path(downloaded["path"]), port)
        yield {"stage": "done", "result": result_install}
        if result_install.get("mode") == "relaunch":
            # 先让响应写完，再退出——替换脚本正等着这个进程消失
            threading.Timer(1.5, lambda: os._exit(0)).start()
    except Exception as exc:
        _log_tail(f"升级失败: {exc}")
        yield {"stage": "error", "error": str(exc)}
    finally:
        with _INSTALL_LOCK:
            _INSTALLING["on"] = False
