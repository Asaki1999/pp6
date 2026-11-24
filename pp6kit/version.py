"""版本与构建指纹：当前这份程序是谁、什么时候打的。

为什么单独一层：
- 关于页原来写死 `1.0.0`，`__init__.__version__` 是 `0.1.0`，`.app` 里又是
  `1.0.0`——三处互相矛盾，用户报问题时根本分不清他装的是哪一版；
- 自查更新必须知道「当前版本」和「构建时间」，否则每天自动发的
  `test0.NN` 无法判断谁新谁旧。

取值优先级：打包时由 `tools/stamp_version.py` 生成的 `_buildinfo.py`
（不入库）→ 源码运行时的 git 信息 → `__version__` 兜底。
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import __version__

# 公共仓库：查最新 Release 与下载附件都走它，匿名可用
DEFAULT_REPO = "Asaki1999/pp6"


def is_frozen() -> bool:
    """是否是 PyInstaller 冻结包（桌面 App / exe）。"""
    return bool(getattr(sys, "frozen", False))


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    """只读 git 查询；没有 git、不是仓库、超时都当成空值。"""
    try:
        out = subprocess.run(
            ["git", *args], cwd=_root(), capture_output=True, text=True, timeout=3,
            encoding="utf-8", errors="replace",
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _stamp() -> dict:
    """读打包时写入的指纹；源码运行没有这个模块。"""
    try:
        from . import _buildinfo  # type: ignore
    except Exception:
        return {}
    return {
        key.lower(): getattr(_buildinfo, key)
        for key in ("VERSION", "COMMIT", "BUILT_AT", "CHANNEL", "REPO")
        if getattr(_buildinfo, key, "")
    }


def build_info() -> dict:
    """版本、提交、构建时间、渠道，以及运行形态。"""
    stamp = _stamp()
    if is_frozen():
        # 冻结包里不能跑 git（没有仓库，也没有 git 可执行文件）
        version = stamp.get("version") or __version__
        commit = stamp.get("commit") or ""
        built_at = stamp.get("built_at") or ""
        channel = stamp.get("channel") or "local"
    else:
        # 源码运行以 git 为准：打包留下的指纹只代表「上次打的那个包」，
        # 之后改了代码还显示旧版本号，排查问题时会被误导
        version = _git("describe", "--tags", "--always", "--dirty") or stamp.get("version") or __version__
        commit = _git("rev-parse", "--short", "HEAD") or stamp.get("commit") or ""
        built_at = _git("show", "-s", "--format=%cI", "HEAD") or stamp.get("built_at") or ""
        channel = "dev"
    return {
        "version": version,
        "commit": commit,
        "builtAt": built_at,
        "channel": channel,
        "frozen": is_frozen(),
        "platform": sys.platform,
        "repo": stamp.get("repo") or DEFAULT_REPO,
    }


# ---------------------------------------------------------------- 版本比较

_NUM = re.compile(r"^[vV]?([A-Za-z]*\d+(?:\.\d+)*)")


def canonical(text: str) -> str:
    """取开头的版本主体，丢掉 git describe 的后缀。

    `test0.23-3-gabc123` → `test0.23`（否则 23-3 会被当成更大的版本）
    `v1.2.3` → `1.2.3`
    `nightly-20261124` → `""`（日期版不按数字比，交给时间戳）
    """
    match = _NUM.match((text or "").strip())
    return match.group(1) if match else ""


def numbers(text: str) -> list[int]:
    return [int(part) for part in re.findall(r"\d+", canonical(text))]


def _date_like(nums: list[int]) -> bool:
    """`20261124` 这种单段八位数是构建日期，不是版本号。"""
    return len(nums) == 1 and nums[0] >= 19000000


def compare_versions(latest: str, current: str) -> int | None:
    """比版本主体：latest 新返回 1，相同 0，旧 -1，无法比较 None。

    日期版（nightly-20261124）返回 None，由调用方改用构建时间判断——
    把 20261124 和 test0.23 硬比只会得出荒谬结论。
    """
    a, b = numbers(latest), numbers(current)
    if not a or not b or _date_like(a) or _date_like(b):
        return None
    width = max(len(a), len(b))
    a += [0] * (width - len(a))
    b += [0] * (width - len(b))
    return (a > b) - (a < b)


def parse_time(text: str) -> datetime | None:
    """解析 ISO 时间；GitHub 给的是 `...Z`，git 给的是 `+08:00`。"""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_newer(latest: str, latest_published: str, current: str, current_built: str,
             tolerance_seconds: int = 90) -> tuple[bool, str]:
    """返回 (是否有新版, 判断依据)。

    容差是必要的：CI 的构建时间早于 Release 发布时间，正常的「同一次构建」
    也会出现几十秒差值，不能当成更新。
    """
    verdict = compare_versions(latest, current)
    if verdict is not None:
        return verdict > 0, "version"
    new_at, old_at = parse_time(latest_published), parse_time(current_built)
    if new_at and old_at:
        return (new_at - old_at).total_seconds() > tolerance_seconds, "time"
    return False, "unknown"
