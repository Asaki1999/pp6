#!/usr/bin/env python3
"""提交前密钥扫描：只扫 Git 跟踪的文件，发现疑似明文 Key 就退出 1。

为什么只扫跟踪文件
------------------
`out/`、`dist/`、`~/.pp6kit` 都不该进仓库；真正会泄露的是被 `git add` 的东西。
2026-11-18 排查过一次：用户以为模型 Key 被推到 GitHub，最后确认
仓库和历史里都没有，明文只在本机被 `.gitignore` 排除的 `out/模型参数-*.json` 里。

为什么要显式 UTF-8
------------------
2026-11-19 Windows CI 在「密钥扫描」步骤 exit 1，macOS 同一步却通过。
根因是本脚本早期版本用 `subprocess.check_output(..., text=True)`：
text 模式按系统 locale 解码，Windows runner 默认 cp1252，
而仓库里有 46 个中文路径，UTF-8 字节里的 0x81 在 cp1252 未定义，
于是抛 UnicodeDecodeError → 掉进 except 的降级分支 → 扫描整个工作区，
把 `.git/`、本机产物等本不该扫的东西也算进来，造成误报。
现在用 `-z` 取 NUL 分隔的 bytes，自己按 UTF-8 解码，与 locale 无关。

用法：
    python tools/check_secrets.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Windows runner 的 stdout 默认 cp1252/ascii，print 中文会抛 UnicodeEncodeError
# 直接让进程 exit 1——2026-11-19 的 Windows CI 就是这么红的（与扫描结果无关）。
# 强制 stdout/stderr 走 UTF-8，与平台 locale 解耦。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PATTERNS = {
    "OpenAI/DeepSeek Key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "AWS Access Key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Google API Key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "GitHub Token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "Private Key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "api_key 赋值": re.compile(r"(?i)api[_-]?key\s*[:=]\s*[\"']([^\"']{20,})[\"']"),
}
# 测试/文档里的占位值，不要误报
ALLOW = ("sk-test", "sk-xxx", "sk-your", "sk-...", "sk-***")
SKIP_FILES = {"tools/check_secrets.py"}

# 降级扫描（git 不可用）时的目录黑名单：产物、缓存、git 元数据都不该进仓库，
# 拿它们做扫描只会误报。2026-11-19 的 Windows CI 误报正是降级分支扫到这类目录。
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "out", "dist", "build"}
# 降级扫描只看可能藏密钥的文本源码；二进制/字体/图片不读
SCAN_SUFFIXES = {".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml",
                 ".toml", ".ini", ".cfg", ".env", ".md", ".txt", ".xml", ".sh",
                 ".bat", ".ps1", ".html", ".css"}


def tracked(root: Path | None = None) -> list[str]:
    """列出待扫描文件：优先 git ls-files，git 不可用时降级扫源码目录。

    `-z` 让 git 用 NUL 分隔路径，既避开空格/换行，也不做引号转义；
    返回的是 bytes，自己按 UTF-8 解码，不受 runner locale 影响。
    """
    try:
        raw = subprocess.check_output(["git", "ls-files", "-z"])
        return [x for x in raw.decode("utf-8", errors="replace").split("\0") if x]
    except Exception:
        pass
    # 降级：只扫源码后缀，且跳过产物/缓存/git 元数据目录
    base = root if root is not None else Path(".")
    return [
        str(p) for p in base.rglob("*")
        if p.is_file()
        and not (set(p.parts) & SKIP_DIRS)
        and p.suffix.lower() in SCAN_SUFFIXES
    ]


def main() -> int:
    files = tracked()
    print(f"密钥扫描：待检查 {len(files)} 个文件", flush=True)
    hits: list[tuple[str, int, str, str]] = []
    for name in files:
        if name in SKIP_FILES:
            continue
        p = Path(name)
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for label, pat in PATTERNS.items():
            for m in pat.finditer(text):
                raw = m.group(0)
                if any(a in raw for a in ALLOW):
                    continue
                # 只露前 6 位，避免扫描日志本身二次泄露
                masked = raw[:6] + "…" + raw[-4:] if len(raw) > 12 else "***"
                line = text[:m.start()].count("\n") + 1
                hits.append((name, line, label, masked))
    if not hits:
        print(f"✓ 密钥扫描通过（{len(files)} 个文件）")
        return 0
    print("✗ 发现疑似明文密钥：", file=sys.stderr)
    for name, line, label, masked in hits[:20]:
        print(f"  {name}:{line}  {label}  {masked}", file=sys.stderr)
    print("请删除/改成占位值后再提交；已提交的要立刻轮换 Key。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
