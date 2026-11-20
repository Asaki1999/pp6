"""桌面壳的原生能力：文件选择框、保存对话框。

为什么必须走原生：WKWebView / WebView2 里没有浏览器那套下载语义，
`<a download>` 点了不会有任何反应；`display:none` 的 file input 也唤不起选择框。
所以文件进出都交给系统对话框，Web 版（bin/pp6 ui）仍走原来的 HTTP 下载。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

EXCEL_TYPES = ("Excel 文件 (*.xlsx;*.xlsm)",)
ZIP_TYPES = ("导出包 (*.zip)",)
SOURCE_TYPES = ("Excel PDF Word 文件 (*.xlsx;*.xlsm;*.pdf;*.docx)",)

# 同一时刻只允许一个原生对话框在跑。
# 根因：界面里「选择文件」曾经既是 <label> 包 <input type=file>（WKWebView 自己
# 会弹一个文件面板，受 App 沙箱限制常看到空目录），又让 JS 调 pick_source 弹
# pywebview 的 NSOpenPanel。两个面板同时 runModal → 用户看到「文件夹为空」和
# 「同时两个选择窗口」，谁在前取决于时序，所以是偶现。前端已经按结构断开了
# 第一条路（index.html 的 #btnReimportPick），这把锁是兜底：即使某处又漏了守卫，
# 也只会有一个原生面板，第二个直接返回 None（等同用户取消）。
_DIALOG_LOCK = threading.Lock()

# 「上次选文件的目录」记忆：选择框总是从上次的位置打开，用户不用每次重新翻目录。
_LAST_DIR_FILE = Path.home() / ".pp6kit" / "last_dir.json"


def _last_dir() -> str:
    """上次用过的目录；读不到 / 不存在 / 已不是目录都静默退回用户主目录。"""
    try:
        raw = json.loads(_LAST_DIR_FILE.read_text(encoding="utf-8")).get("dir") or ""
        if isinstance(raw, str) and raw.strip():
            path = Path(raw).expanduser()
            if path.is_dir():
                return str(path)
    except Exception:
        pass          # 配置坏了不能影响选文件，静默降级
    return str(Path.home())


def _remember_dir(path: Path) -> None:
    """记住这次选中文件所在的目录（写失败一律静默）。"""
    try:
        parent = Path(path).expanduser().resolve().parent
        if not parent.is_dir():
            return
        _LAST_DIR_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LAST_DIR_FILE.write_text(json.dumps({"dir": str(parent)}, ensure_ascii=False),
                                  encoding="utf-8")
    except Exception:
        pass


def _dialog(kind: str):
    """pywebview 6 用 FileDialog 枚举，旧版用模块常量。"""
    import webview

    enum = getattr(webview, "FileDialog", None)
    if enum is not None:
        return enum.OPEN if kind == "open" else enum.SAVE
    return webview.OPEN_DIALOG if kind == "open" else webview.SAVE_DIALOG


class DesktopApi:
    """暴露给 JS：window.pywebview.api.xxx()"""

    def __init__(self) -> None:
        self.window = None

    def bind(self, window) -> None:
        self.window = window

    # ---- 内部
    def _open(self, kinds: tuple[str, ...]) -> dict | None:
        # 已经有一个对话框在跑：不再叠第二个，直接当「用户取消」（见 _DIALOG_LOCK）
        if not _DIALOG_LOCK.acquire(blocking=False):
            return None
        try:
            files = self.window.create_file_dialog(
                _dialog("open"), allow_multiple=False, file_types=kinds,
                directory=_last_dir(),
            )
            if not files:
                return None
            path = Path(files[0] if isinstance(files, (list, tuple)) else files)
            if not path.is_file():
                return None
            _remember_dir(path)
            return {"name": path.name, "data": base64.b64encode(path.read_bytes()).decode("ascii")}
        finally:
            _DIALOG_LOCK.release()

    def _save(self, filename: str, data_b64: str, file_type: str) -> dict | None:
        # 自动化自检：跳过对话框直接落盘，生产环境此变量不会存在
        forced = os.environ.get("PP6_SELFTEST_SAVE")
        if forced:
            path = Path(forced)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(data_b64))
            return {"path": str(path)}

        if not _DIALOG_LOCK.acquire(blocking=False):
            return None
        try:
            picked = self.window.create_file_dialog(
                _dialog("save"), save_filename=filename, file_types=(file_type,),
                directory=_last_dir(),
            )
            if not picked:
                return None
            target = picked if isinstance(picked, str) else picked[0]
            path = Path(target)
            if path.suffix == "" and Path(filename).suffix:
                path = path.with_suffix(Path(filename).suffix)
            path.write_bytes(base64.b64decode(data_b64))
            _remember_dir(path)
            return {"path": str(path)}
        finally:
            _DIALOG_LOCK.release()

    # ---- 暴露给前端
    def pick_excel(self) -> dict | None:
        return self._open(EXCEL_TYPES)

    def pick_source(self) -> dict | None:
        """AI 入口的文件选择：Excel 读表，PDF/Word 抽文字。"""
        return self._open(SOURCE_TYPES)

    def pick_zip(self) -> dict | None:
        return self._open(ZIP_TYPES)

    def save_file(self, filename: str, data_b64: str) -> dict | None:
        suffix = Path(filename).suffix.lower()
        file_type = "ZIP 压缩包 (*.zip)" if suffix == ".zip" else "Excel 文件 (*.xlsx)"
        return self._save(filename, data_b64, file_type)

    # ---------------------------------------------------------------- 剪贴板
    def copy_text(self, text: str) -> dict:
        """写系统剪贴板。WebView 的 navigator.clipboard 在部分环境不可用，
        桌面模式优先走这条原生通道。"""
        if not isinstance(text, str) or not text:
            return {"ok": False, "error": "没有内容可复制"}
        try:
            if sys.platform == "darwin":
                cmd = ["pbcopy"]
            elif os.name == "nt":
                cmd = ["clip"]
            elif shutil.which("wl-copy"):
                cmd = ["wl-copy"]
            elif shutil.which("xclip"):
                cmd = ["xclip", "-selection", "clipboard"]
            else:
                return {"ok": False, "error": "系统没有可用的剪贴板命令"}
            # 用 bytes 传，避免不同平台 locale 把中文弄坏
            subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=5)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
