"""打包后的资源定位。

PyInstaller 冻结后 __file__ 指向临时解包目录，源码目录结构不再成立，
必须统一走这里，否则 templates/assets 会找不到。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def resource_root() -> Path:
    """只读资源根目录（assets/、pp6kit/static/）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


def snippets_path() -> Path:
    return resource_root() / "assets" / "snippets.json"


def work_dir() -> Path:
    """可写临时目录（上传的临时文件、生成中间产物）。"""
    path = Path(tempfile.gettempdir()) / "pp6kit-work"
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_dir() -> Path:
    """CLI 的默认输出目录：当前工作目录下的 out/。"""
    path = Path.cwd() / "out"
    path.mkdir(parents=True, exist_ok=True)
    return path
