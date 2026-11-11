"""桌面壳的原生能力：文件选择框、保存对话框。

为什么必须走原生：WKWebView / WebView2 里没有浏览器那套下载语义，
`<a download>` 点了不会有任何反应；`display:none` 的 file input 也唤不起选择框。
所以文件进出都交给系统对话框，Web 版（bin/pp6 ui）仍走原来的 HTTP 下载。
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

EXCEL_TYPES = ("Excel 文件 (*.xlsx;*.xlsm)",)
ZIP_TYPES = ("导出包 (*.zip)",)


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
        files = self.window.create_file_dialog(
            _dialog("open"), allow_multiple=False, file_types=kinds
        )
        if not files:
            return None
        path = Path(files[0] if isinstance(files, (list, tuple)) else files)
        if not path.is_file():
            return None
        return {"name": path.name, "data": base64.b64encode(path.read_bytes()).decode("ascii")}

    def _save(self, filename: str, data_b64: str, file_type: str) -> dict | None:
        # 自动化自检：跳过对话框直接落盘，生产环境此变量不会存在
        forced = os.environ.get("PP6_SELFTEST_SAVE")
        if forced:
            path = Path(forced)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(data_b64))
            return {"path": str(path)}

        picked = self.window.create_file_dialog(
            _dialog("save"), save_filename=filename, file_types=(file_type,)
        )
        if not picked:
            return None
        target = picked if isinstance(picked, str) else picked[0]
        path = Path(target)
        if path.suffix == "" and Path(filename).suffix:
            path = path.with_suffix(Path(filename).suffix)
        path.write_bytes(base64.b64decode(data_b64))
        return {"path": str(path)}

    # ---- 暴露给前端
    def pick_excel(self) -> dict | None:
        return self._open(EXCEL_TYPES)

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
