"""写入构建指纹：`pp6kit/_buildinfo.py`（生成物，不入库）。

为什么要有这一步：
- 版本号必须来自「这次构建实际是什么」，而不是人记得改的常量；
- 关于页、更新检查、问题排查（日志里的版本）都读同一份。

用法：
    python tools/stamp_version.py                     # 本地：git describe + 短提交
    python tools/stamp_version.py --version test0.42 --channel release
    python tools/stamp_version.py --check             # 只打印，不落盘
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "pp6kit" / "_buildinfo.py"
DEFAULT_REPO = "Asaki1999/pp6"


def git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                             text=True, timeout=5, encoding="utf-8", errors="replace")
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="写入 pp6kit/_buildinfo.py")
    ap.add_argument("--version", default="", help="版本名，默认取 git describe")
    ap.add_argument("--commit", default="", help="提交短哈希，默认取 HEAD")
    ap.add_argument("--channel", default="", help="release / nightly / dev")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="GitHub 仓库 owner/name")
    ap.add_argument("--out", default=str(TARGET), help="输出文件")
    ap.add_argument("--check", action="store_true", help="只打印，不写文件")
    args = ap.parse_args(argv)

    version = args.version or git("describe", "--tags", "--always", "--dirty")
    if not version:
        version = "dev-" + datetime.now().strftime("%Y%m%d")
    commit = args.commit or git("rev-parse", "--short", "HEAD") or "unknown"
    channel = args.channel or ("dev" if version.startswith("dev") else
                               "nightly" if version.startswith("nightly") else "release")
    built_at = datetime.now().astimezone().isoformat(timespec="seconds")

    body = (
        '"""本次构建的指纹，由 tools/stamp_version.py 生成，勿手工编辑。"""\n'
        f"VERSION = {version!r}\n"
        f"COMMIT = {commit!r}\n"
        f"BUILT_AT = {built_at!r}\n"
        f"CHANNEL = {channel!r}\n"
        f"REPO = {args.repo!r}\n"
    )
    print(f"版本 {version} · 提交 {commit} · 渠道 {channel} · {built_at}")
    if args.check:
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(f"已写入 {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
