# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 pp6kit 打成一个不自带 Python 的桌面程序。

macOS 产出 .app（原生 WKWebView 窗口），Windows 产出 .exe（Edge WebView2 窗口）。
两者都内置静态页面与控件骨架，目标机器无需安装 Python / 浏览器插件。
"""
import os
import sys
from pathlib import Path

ROOT = Path(os.getcwd())

# 版本来自 tools/stamp_version.py 生成的指纹（构建脚本会先写）。
# 没有指纹（例如直接跑 pyinstaller）就退回 0.0.0，至少不会伪装成 1.0.0。
_STAMP = ROOT / "pp6kit" / "_buildinfo.py"
STAMP: dict = {}
if _STAMP.exists():
    exec(compile(_STAMP.read_text(encoding="utf-8"), str(_STAMP), "exec"), STAMP)
APP_VERSION = str(STAMP.get("VERSION") or "0.0.0")
_BUILT = str(STAMP.get("BUILT_AT") or "")
# CFBundleVersion 只接受数字与点：用构建日期，缺省 1
BUNDLE_VERSION = "".join(ch for ch in _BUILT[:10] if ch.isdigit()) or "1"

datas = [
    (str(ROOT / "pp6kit" / "static"), "pp6kit/static"),
    (str(ROOT / "assets" / "snippets.json"), "assets"),
    # AI 的 few-shot 语料（198 条已验证问答对），由 tools/build_ai_corpus.py 生成
    (str(ROOT / "assets" / "ai_examples.json"), "assets"),
    # 业务对象清单快照（关联对象的下拉来源），由 tools/dump_objects.py 生成
    (str(ROOT / "assets" / "objects.json"), "assets"),
    # 对象关系历史快照：离线识别「新对象该挂到哪个已有对象下」的证据，
    # 由 tools/ 脚本从 obj_relationship 表 dump（与 objects.refresh 同源）
    (str(ROOT / "assets" / "relationships.json"), "assets"),
    # 字段类型字典（dataTypeNew -> datatype/length/format）。漏掉它冻结态
    # fieldtypes.specs() 会整体 fallback 成 varchar/200，字段精度全丢
    (str(ROOT / "assets" / "fieldtypes.json"), "assets"),
    # 数据库画像与追问语料（nlq/probe 用）
    (str(ROOT / "assets" / "dbprofile.json"), "assets"),
    (str(ROOT / "assets" / "probe_questions.json"), "assets"),
    # 程序图标：Windows exe / 托盘菜单共用（winapp.executable_icon 按这个路径找）
    (str(ROOT / "assets" / "app.ico"), "assets"),
]

hiddenimports = [
    "openpyxl",
    "openpyxl.cell._writer",
    "certifi",
]

if sys.platform == "darwin":
    # macOS 的应用窗口走 pywebview + 系统 WKWebView。Windows 不走这条：
    # 那边用 Edge/Chrome 的 --app 模式（pp6kit/winapp.py），打包也就不带
    # pywebview / pythonnet——pythonnet 在冻结包里卡在 CLR 初始化上，
    # 正是历史上 Windows 版「未响应」的来源。
    hiddenimports += ["webview", "webview.platforms.cocoa"]

# 更新检查走 HTTPS：certifi 的 cacert.pem 必须随包，否则冻结态容易
# CERTIFICATE_VERIFY_FAILED（系统证书路径各家机器不一样）
try:
    import certifi as _certifi
    datas.append((_certifi.where(), "certifi"))
except Exception:
    pass

excludes = [
    "tkinter", "matplotlib", "numpy", "pandas", "PIL", "PyQt5", "PyQt6",
    "PySide2", "PySide6", "pytest", "IPython", "notebook", "scipy",
]

if sys.platform == "win32":
    # Windows 的应用窗口走 Edge/Chrome 的 --app 模式（pp6kit/winapp.py），
    # 完全不需要 pywebview；而 webview 会顺着函数内的延迟 import 被分析到，
    # 再把 pythonnet（.NET 互操作）拖进包里——那正是历史上「未响应」的来源。
    # 这里显式排除：exe 里没有 .NET，也不会有那条初始化路径。
    excludes += ["webview", "clr", "pythonnet"]

a = Analysis(
    [str(ROOT / "app_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PP6ImportBuilder",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    # Windows 的 exe 图标与托盘图标同源；macOS 走 BUNDLE 的 .icns
    icon=str(ROOT / "assets" / "app.ico") if (sys.platform == "win32" and (ROOT / "assets" / "app.ico").exists()) else None,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PP6ImportBuilder",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="PP6 Studio.app",
        icon=str(ROOT / "assets" / "app.icns") if (ROOT / "assets" / "app.icns").exists() else None,
        bundle_identifier="com.powerproject.pp6kit",
        info_plist={
            "CFBundleName": "PP6 Studio",
            "CFBundleDisplayName": "PP6 Studio",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": BUNDLE_VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
