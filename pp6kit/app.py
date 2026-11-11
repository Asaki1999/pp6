"""桌面应用入口。

窗口用系统自带 WebView（macOS: WKWebView / Windows: Edge WebView2），
界面仍是本地 HTML，但用户看到的是一个独立应用窗口，不是浏览器标签页。
文件读写走原生对话框（见 desktop_api），不依赖浏览器下载语义。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from pathlib import Path

from .desktop_api import DesktopApi
from .webui import start_background

APP_NAME = "PP6 导入包生成器"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pp6kit-app")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--headless", action="store_true", help="只起服务不开窗口（自检用）")
    args = ap.parse_args(argv)

    httpd, url = start_background(args.port)

    if args.headless:
        print(url, flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            httpd.shutdown()
        return 0

    try:
        import webview  # type: ignore
    except ImportError:
        print(f"{APP_NAME}: 未安装 pywebview，改用浏览器打开")
        return _run_in_browser(url, httpd)

    api = DesktopApi()
    try:
        window = webview.create_window(
            APP_NAME,
            url,
            width=1240,
            height=920,
            min_size=(1000, 700),
            text_select=True,
            js_api=api,
        )
        api.bind(window)
    except Exception as exc:
        print(f"{APP_NAME}: 无法创建窗口（{exc}），改用浏览器打开")
        return _run_in_browser(url, httpd)

    if os.environ.get("PP6_SELFTEST") == "1":
        threading.Thread(target=_selftest, args=(window,), daemon=True).start()

    try:
        webview.start()  # 阻塞直到窗口关闭
    except Exception as exc:
        print(f"{APP_NAME}: 窗口启动失败（{exc}），改用浏览器打开")
        return _run_in_browser(url, httpd)
    finally:
        httpd.shutdown()
    return 0


def _clip_get() -> str:
    try:
        if sys.platform == "darwin":
            return subprocess.run(["pbpaste"], capture_output=True, timeout=5).stdout.decode("utf-8", "ignore")
        if os.name == "nt":
            return subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                                  capture_output=True, timeout=8).stdout.decode("utf-8", "ignore").rstrip("\r\n")
    except Exception:
        pass
    return ""


def _clip_set(text: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5)
        elif os.name == "nt":
            subprocess.run(["clip"], input=text.encode("utf-8"), timeout=5)
    except Exception:
        pass


def _selftest(window) -> None:
    """冻结包自检：确认 JS 能跑、原生桥接已注入、示例能解析、复制能落剪贴板。"""
    backup = _clip_get()
    probe = "PP6-SELFTEST-复制探针-中文"
    try:
        time.sleep(3.5)
        checks = {
            "js_alive": window.evaluate_js("typeof handleFile === 'function'"),
            "bridge": window.evaluate_js("!!(window.pywebview && window.pywebview.api)"),
            "pick_excel": window.evaluate_js("typeof (window.pywebview && window.pywebview.api.pick_excel)"),
            "save_file": window.evaluate_js("typeof (window.pywebview && window.pywebview.api.save_file)"),
            "copy_text": window.evaluate_js("typeof (window.pywebview && window.pywebview.api.copy_text)"),
            "desktop_flag": window.evaluate_js("!!window.PP6_DESKTOP"),
        }
        window.evaluate_js("document.getElementById('btnDemo') && document.getElementById('btnDemo').click()")
        time.sleep(2.5)
        checks["demo_loaded"] = window.evaluate_js("!document.getElementById('result').hidden")
        checks["stat_first"] = window.evaluate_js("document.querySelector('#stats .n') && document.querySelector('#stats .n').textContent")

        forced = os.environ.get("PP6_SELFTEST_SAVE")
        if forced:
            window.evaluate_js("document.getElementById('btnBuild') && document.getElementById('btnBuild').click()")
            time.sleep(4)
            out = Path(forced)
            checks["saved"] = out.exists()
            checks["saved_size"] = out.stat().st_size if out.exists() else 0
            checks["saved_is_zip"] = bool(zipfile.is_zipfile(out)) if out.exists() else False
        # 真机验证「复制 SQL」：走前端 → 原生桥接 → 系统剪贴板
        _clip_set("")
        window.evaluate_js(
            "setTimeout(function(){"
            f"window.pywebview.api.copy_text({json.dumps(probe, ensure_ascii=False)})"
            "},0); true"
        )
        time.sleep(1.5)
        checks["clip_ok"] = _clip_get() == probe

        print("SELFTEST " + " ".join(f"{k}={v}" for k, v in checks.items()), flush=True)
    except Exception as exc:
        print(f"SELFTEST FAILED {exc}", flush=True)
    finally:
        _clip_set(backup)  # 还原用户剪贴板
        try:
            window.destroy()
        except Exception:
            pass
        os._exit(0)


def _run_in_browser(url: str, httpd) -> int:
    print(f"{APP_NAME} 已启动: {url}")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
