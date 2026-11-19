#!/usr/bin/env python3
"""提交前密钥扫描：只扫 Git 跟踪的文件，发现疑似明文 Key 就退出 1。

为什么只扫跟踪文件
------------------
`out/`、`dist/`、`~/.pp6kit` 都不该进仓库；真正会泄露的是被 `git add` 的东西。
2026-11-18 排查过一次：用户以为模型 Key 被推到 GitHub，最后确认
仓库和历史里都没有，明文只在本机被 `.gitignore` 排除的 `out/模型参数-*.json` 里。

用法：
    python tools/check_secrets.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

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


def tracked() -> list[str]:
    try:
        out = subprocess.check_output(["git", "ls-files"], text=True)
    except Exception:
        return [str(p) for p in Path(".").rglob("*") if p.is_file()]
    return [x for x in out.splitlines() if x]


def main() -> int:
    hits: list[tuple[str, int, str, str]] = []
    for name in tracked():
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
        print(f"✓ 密钥扫描通过（{len(tracked())} 个跟踪文件）")
        return 0
    print("✗ 发现疑似明文密钥：", file=sys.stderr)
    for name, line, label, masked in hits[:20]:
        print(f"  {name}:{line}  {label}  {masked}", file=sys.stderr)
    print("请删除/改成占位值后再提交；已提交的要立刻轮换 Key。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
