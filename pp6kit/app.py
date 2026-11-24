"""桌面应用入口。

窗口用系统自带 WebView（macOS: WKWebView / Windows: Edge WebView2），
界面仍是本地 HTML，但用户看到的是一个独立应用窗口，不是浏览器标签页。
文件读写走原生对话框（见 desktop_api），不依赖浏览器下载语义。

关于启动顺序：`console=False` 的打包产物没有控制台，所以日志必须最先就位，
重模块的导入也放到日志之后——否则导入阶段崩掉就只剩一个卡死的窗口，查不到原因。
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

from . import applog, instance

APP_NAME = "PP6 Studio"
DEFAULT_PORT = instance.DEFAULT_PORT

# 窗口迟迟不加载完就把同一地址丢给浏览器，至少能用
LOAD_GRACE_SECONDS = 20


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pp6kit-app")
    ap.add_argument("--port", type=int, default=None,
                    help="固定端口（默认 8770，被占则顺延；显式指定时不启用单实例接管）")
    ap.add_argument("--headless", action="store_true", help="只起服务不开窗口（自检用）")
    ap.add_argument("--window", action="store_true",
                    help="用系统 WebView 窗口打开（Windows 需显式指定）")
    ap.add_argument("--browser", action="store_true", help="不用应用窗口，直接在浏览器打开")
    args = ap.parse_args(argv)

    applog.setup()
    log = applog.get("pp6kit.app")

    # 模式先定，后面每一步都按它走。
    # Windows 默认浏览器：WebView2 + pythonnet 这条链在冻结包里极易卡死在主线程，
    # 用户看到的就是「未响应」；而界面本来就是本地网页，浏览器打开功能完全一致。
    if args.headless:
        mode = "headless"
    elif args.browser:
        mode = "browser"
    elif args.window:
        mode = "window"
    elif os.name == "nt":
        mode = "browser"
    else:
        mode = "window"

    log.info("=" * 64)
    log.info("%s 启动，参数 %s，模式 %s", APP_NAME, sys.argv[1:], mode)
    applog.dump_env(probe_gui=(mode == "window"))

    # 这两个模块会连带导入 ai_* 全家族（含 sqlglot 等重依赖），
    # 放在日志就位之后，导入失败也能留下痕迹。
    try:
        from .webui import start_background
    except Exception:
        applog.fatal("加载服务模块失败（依赖缺失或打包不完整）")
        return 2

    # 单实例：默认桌面启动才管。--headless / 显式 --port 是自检、CI 与多开调试，
    # 必须能和用户手上那份并存，所以整段跳过。
    port = DEFAULT_PORT if args.port is None else args.port
    single = mode != "headless" and args.port is None and os.environ.get("PP6_SELFTEST") != "1"
    claim = None
    if single:
        claim = instance.claim(instance.build_key())
        if claim.status == "reused":
            # 同一份程序已经在跑：不再起第二个服务，把用户送回那个页面
            log.info("已有同版本实例在运行：%s（%s），不再重复启动",
                     claim.url, claim.detail)
            print(f"{APP_NAME} 已在运行: {claim.url}", flush=True)
            if mode == "browser":
                try:
                    webbrowser.open(claim.url)
                except Exception:
                    log.warning("打开已有实例的页面失败", exc_info=True)
            return 0
        if claim.status == "blocked":
            log.error("无法接管已在运行的实例：%s", claim.detail)
            applog.alert(
                "已有一个 PP6 在运行",
                "检测到旧实例还在运行，但它没有响应退出请求。\n\n"
                f"{claim.detail}\n\n"
                "请先在任务管理器里结束 PP6ImportBuilder.exe，再重新打开。\n"
                f"日志：{applog.log_path()}")
            return 4
        if claim.status == "took-over":
            log.info("已让旧实例（%s）退出，接管端口 %d", claim.detail, claim.port)
        if claim.port:
            # 沿用旧端口：浏览器里已经开着的标签刷新一下就是新版界面
            port = claim.port

    try:
        httpd, url = start_background(port)
    except Exception:
        applog.fatal("HTTP 服务启动失败，端口 %d" % port)
        return 3
    if single and claim is not None and claim.ok:
        instance.register(httpd.server_address[1], httpd.instance_token)
        log.info("实例已登记：pid %d / 端口 %d", os.getpid(), httpd.server_address[1])
    log.info("本地服务就绪 -> %s（启动后 %.2f 秒）", url, applog.elapsed())

    if mode == "headless":
        print(url, flush=True)
        log.info("headless 模式：只提供 HTTP 服务，不创建窗口")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            httpd.shutdown()
        return 0

    if mode == "browser":
        if os.name == "nt" and not args.browser:
            log.info("Windows 默认使用浏览器模式：避开 WebView2/pythonnet 在冻结包中"
                     "初始化卡死的问题（可用 --window 强制应用窗口）")
        else:
            log.info("按参数要求跳过应用窗口，直接使用浏览器")
        return _run_in_browser(url, httpd)

    try:
        import webview  # type: ignore
    except Exception:
        log.warning("导入 pywebview 失败，改用浏览器打开", exc_info=True)
        return _run_in_browser(url, httpd)

    # WebView2 缺失时创建窗口会白屏甚至卡死（用户看到「未响应」）。
    # 这种情况直接走浏览器——功能完全一样，只是没有独立窗口。
    if os.name == "nt":
        state = applog.webview2_state()
        if state is False:
            log.warning("系统未检测到 WebView2 运行时，跳过窗口直接使用浏览器")
            applog.alert(
                "缺少 WebView2 组件",
                "系统未安装 Microsoft Edge WebView2 运行时，无法创建应用窗口。\n\n"
                "程序已改用默认浏览器打开，功能不受影响。\n"
                "如需应用窗口，可从微软官网安装 WebView2 运行时后重新打开。")
            return _run_in_browser(url, httpd)
        log.info("WebView2 运行时检测：%s", {True: "已安装", None: "无法判断"}.get(state))

    try:
        from .desktop_api import DesktopApi
    except Exception:
        log.warning("加载原生对话框模块失败，改用浏览器打开", exc_info=True)
        return _run_in_browser(url, httpd)

    api = DesktopApi()
    try:
        log.info("准备创建应用窗口 %dx%d", 1240, 920)
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
        # 另一个实例请求接管时，先关窗口让 webview.start() 返回，
        # 再由本函数的 finally 关服务；直接 os._exit 会丢日志。
        httpd.on_quit = window.destroy
        log.info("窗口对象已创建（启动后 %.2f 秒）", applog.elapsed())
    except Exception:
        log.warning("无法创建窗口，改用浏览器打开", exc_info=True)
        return _run_in_browser(url, httpd)

    loaded = {"ok": False}

    def _on_loaded() -> None:
        loaded["ok"] = True
        log.info("界面加载完成（启动后 %.2f 秒）", applog.elapsed())

    try:
        window.events.loaded += _on_loaded
    except Exception:
        log.warning("未能挂接 loaded 事件，跳过加载完成检测", exc_info=True)

    def _watch_load() -> None:
        """窗口一直不出内容时，把同一地址交给浏览器，别让用户对着白屏干等。"""
        time.sleep(LOAD_GRACE_SECONDS)
        if loaded["ok"]:
            return
        log.warning("窗口 %d 秒内未完成加载，改为在默认浏览器打开同一地址",
                    LOAD_GRACE_SECONDS)
        try:
            webbrowser.open(url)
        except Exception:
            log.warning("调用默认浏览器失败", exc_info=True)

    threading.Thread(target=_watch_load, name="load-watch", daemon=True).start()

    if os.environ.get("PP6_SELFTEST") == "1":
        threading.Thread(target=_selftest, args=(window,), daemon=True).start()

    try:
        log.info("进入窗口事件循环（启动后 %.2f 秒）", applog.elapsed())
        webview.start()  # 阻塞直到窗口关闭
        log.info("窗口已关闭（运行 %.1f 秒）", applog.elapsed())
    except Exception:
        log.warning("窗口事件循环异常退出，改用浏览器打开", exc_info=True)
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
        applog.get("pp6kit.app").info("SELFTEST %s",
                                      " ".join(f"{k}={v}" for k, v in checks.items()))
    except Exception as exc:
        print(f"SELFTEST FAILED {exc}", flush=True)
        applog.get("pp6kit.app").warning("SELFTEST FAILED", exc_info=True)
    finally:
        _clip_set(backup)  # 还原用户剪贴板
        try:
            window.destroy()
        except Exception:
            pass
        os._exit(0)


def _run_in_browser(url: str, httpd) -> int:
    log = applog.get("pp6kit.app")
    log.info("浏览器模式已启动: %s", url)
    print(f"{APP_NAME} 已启动: {url}", flush=True)
    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception:
        log.warning("调用默认浏览器失败", exc_info=True)

    if not opened:
        # 双击 exe 的场景没有控制台，浏览器没弹出来就等于「点了没反应」
        log.error("未能自动打开浏览器，请手动访问 %s", url)
        applog.alert("请手动打开浏览器",
                     f"程序已启动，但没能自动打开浏览器。\n\n"
                     f"请手动访问：\n{url}\n\n"
                     f"日志：{applog.log_path()}")
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
