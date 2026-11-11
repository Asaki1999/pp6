"""把源码里的静态资源同步进已打包的 App。

为什么需要
----------
PyInstaller 会把 pp6kit/static 打进 dist/，之后改源码**不会**自动生效。
忘了重新打包就会出现「代码明明改了，界面还是旧的」——排查半天。

本脚本只复制 4 个纯文本文件（html/js/css），几秒完成，
适合改界面时快速验证；改动到 .py 逻辑时才需要真正重新打包。

用法：
    python tools/sync_static.py          # 同步到所有已打包目标
    python tools/sync_static.py --check  # 只检查是否一致，不写
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "pp6kit" / "static"
FILES = ("index.html", "app.js", "query.js", "style.css", "relations.js")

# 打包产物的两种形态：macOS .app 和 onedir 目录
TARGETS = (
    ROOT / "dist" / "PP6 导入包生成器.app" / "Contents" / "Resources" / "pp6kit" / "static",
    ROOT / "dist" / "PP6ImportBuilder" / "_internal" / "pp6kit" / "static",
)


def digest(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest() if p.exists() else ""


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    if not SRC.is_dir():
        print(f"源码静态目录不存在: {SRC}")
        return 2

    found = [t for t in TARGETS if t.parent.exists()]
    if not found:
        print("没找到已打包的产物（dist/ 下没有 .app 或 PP6ImportBuilder）")
        print("先执行一次打包：见 README「重新打包」")
        return 2

    stale_total = 0
    for target in found:
        label = target.parent.parent.parent.name if ".app" in str(target) else target.parent.name
        print(f"\n{label}")
        for name in FILES:
            src, dst = SRC / name, target / name
            if not src.exists():
                print(f"  ? {name}: 源码里没有，跳过")
                continue
            same = digest(src) == digest(dst)
            if same:
                print(f"  = {name}: 一致")
                continue
            stale_total += 1
            if check_only:
                print(f"  ! {name}: 不一致（需要同步）")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                print(f"  ✓ {name}: 已同步")

    if check_only:
        print(f"\n{'全部一致' if stale_total == 0 else f'{stale_total} 个文件需要同步'}")
        return 0 if stale_total == 0 else 1

    print(f"\n{'没有需要更新的文件' if stale_total == 0 else f'已同步 {stale_total} 个文件'}")
    if stale_total:
        print("重启 App 生效（改的是 html/js/css，不用重新打包）")
        print("注意：如果改的是 .py 逻辑，必须重新打包才生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
