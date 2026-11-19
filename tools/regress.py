"""PP6 Studio 回归测试运行器（零依赖）。

用法：
    python tools/regress.py                 # 跑全部用例
    python tools/regress.py --list          # 只列清单（按编号）
    python tools/regress.py -m patch        # 只跑某个模块
    python tools/regress.py --only HTTP     # 按编号前缀过滤
    python tools/regress.py --level P0      # 只跑 P0
    python tools/regress.py -x              # 首个失败即停
    python tools/regress.py --report docs/测试用例清单.md   # 重写用例清单

退出码：0 全通过（允许 skip）/ 1 有失败。
"""
from __future__ import annotations

import argparse
import importlib
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import _harness as H          # noqa: E402

TEST_DIR = ROOT / "tests"

# 模块顺序 = 清单顺序 = 从底层到端到端，出问题先看最前面的
# 新模块加在这里才会被加载——**这是最容易漏的一步**（用例写了但没跑）
MODULES = ["naming", "pm_terms", "reader", "doc_text", "desktop", "validate", "bo", "projlink",
           "form", "tablefield", "builder", "workflow", "patch", "webui", "security", "nlq"]

# 本次回归会话独有的临时根目录（见 _isolate_tmp）
_TMP_DIR: Path | None = None


def _isolate_tmp() -> Path:
    """给本次回归会话一个**独立**的 TMPDIR，避免与别人并发跑时互相踩。

    为什么必须这么做（2026-11-13 实测）：
      ``resources.work_dir()`` = ``Path(tempfile.gettempdir()) / "pp6kit-work"``，
      而 ``webui.build_zip()`` 在里面用**固定文件名** ``.build.xlsx`` /
      ``.upload.xlsx`` …，每个调用都在 ``finally`` 里删掉它。
      两条回归流水线同时跑 → A 的 ``finally`` 删掉 B 正在读的文件，
      表现为随机几条报 ``[Errno 2] ... .build.xlsx`` / ``Truncated file header``
      / ``File is not a zip file``（QA 与 team-lead 各踩过一次，6 条随机红）。

    修法选择：**只隔离测试**，不改 ``work_dir()``、也不改那 9 个固定临时路径。
    产品侧的「桌面壳双开互删工作目录」是另一个问题，留作独立修复
    ——注意那一版的修复清单必须包含 ``webui.workflow_files()`` 的
    ``workflow-out/``（它 ``rmtree`` 整个目录，杀伤面比单个文件更大），
    只改 ``build_zip`` 的几处固定名属于假修。

    ⚠ ``tempfile.gettempdir()`` 会把首次结果缓存在 ``tempfile.tempdir``，
    所以除了设环境变量还要直接赋 ``tempfile.tempdir``；并且必须在**任何**
    ``work_dir()`` 调用之前执行（``load()`` 是最早的入口）。
    """
    global _TMP_DIR
    path = Path(tempfile.mkdtemp(prefix="pp6kit-tmp-"))
    os.environ["TMPDIR"] = str(path)          # macOS / Linux
    os.environ["TEMP"] = str(path)            # Windows
    os.environ["TMP"] = str(path)
    tempfile.tempdir = str(path)              # 兜底：可能已被缓存
    _TMP_DIR = path
    return path


def _cleanup_tmp() -> None:
    """删掉会话临时目录，别把 /tmp 堆满。失败不影响退出码。"""
    global _TMP_DIR
    if _TMP_DIR is not None:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
        _TMP_DIR = None


def load() -> None:
    # 必须在 import 测试模块（它们会 import pp6kit → 可能算 work_dir）之前
    _isolate_tmp()
    # 系统 python 往往没装 openpyxl，报错会是一长串 import 栈，不如直接说清楚
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        print("依赖不全（缺 openpyxl）。请用项目虚拟环境跑：\n"
              "    ./.venv/bin/python tools/regress.py")
        raise SystemExit(2) from None
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for mod in MODULES:
        path = TEST_DIR / f"test_{mod}.py"
        if not path.is_file():
            continue
        importlib.import_module(f"tests.test_{mod}")


def select(cases, only: str, module: str, level: str):
    out = cases
    if module:
        out = [c for c in out if c.module == module]
    if only:
        out = [c for c in out if c.cid.upper().startswith(only.upper())]
    if level:
        out = [c for c in out if c.level.upper() == level.upper()]
    return out


def run(cases, fail_fast: bool) -> tuple[int, int, int]:
    ok = bad = skipped = 0
    failures: list[tuple[str, str, str]] = []
    width = max((len(c.cid) for c in cases), default=4)
    started = time.time()
    for c in cases:
        t0 = time.perf_counter()
        try:
            c.fn()
            cost = (time.perf_counter() - t0) * 1000
            print(f"  ✓ {c.cid:<{width}} {c.title}  ({cost:.0f}ms)")
            ok += 1
        except H.Skip as exc:
            print(f"  ~ {c.cid:<{width}} {c.title}  — 跳过：{exc}")
            skipped += 1
        except Exception as exc:                       # noqa: BLE001
            detail = str(exc) or exc.__class__.__name__
            print(f"  ✗ {c.cid:<{width}} {c.title}")
            for line in detail.splitlines()[:6]:
                print(f"      {line}")
            failures.append((c.cid, c.title, detail))
            bad += 1
            if fail_fast:
                print("\n首个失败即停（-x）。")
                break
    cost = time.time() - started
    print(f"\n合计 {ok + bad + skipped} 条：通过 {ok}，失败 {bad}，跳过 {skipped}"
          f"，耗时 {cost:.1f}s")
    if failures:
        print("\n失败明细：")
        for cid, title, detail in failures:
            print(f"  {cid}  {title}")
            for line in detail.splitlines()[:8]:
                print(f"      {line}")
    return ok, bad, skipped


def write_report(path: Path, cases) -> None:
    """从注册表生成用例清单，保证文档和代码不会各说一套。"""
    by_module: dict[str, list] = {}
    for c in cases:
        by_module.setdefault(c.module, []).append(c)

    label = {"P0": "核心", "P1": "常规", "P2": "边界"}
    lines = [
        "# PP6 Studio 回归用例清单",
        "",
        "> 本文件由 `python tools/regress.py --report docs/测试用例清单.md` 生成，",
        "> 内容直接来自 `tests/` 里的用例注册表——**不要手改**，改了会在下次生成时被覆盖。",
        "",
        f"共 {len(cases)} 条；核心链路 {sum(1 for c in cases if c.level == 'P0')} 条。",
        "",
        "## 什么时候跑",
        "",
        "- 每次往 app 里迁功能（改 `pp6kit/`、`assets/`、`static/`）→ 全量跑一遍；",
        "- 只改前端 → 至少跑 `-m webui -m security`；",
        "- 新增功能 → 在对应 `tests/test_*.py` 里加用例（见文末「加用例的规矩」）。",
        "",
        "```bash",
        "./.venv/bin/python tools/regress.py            # 全量",
        "./.venv/bin/python tools/regress.py --list     # 只看清单",
        "./.venv/bin/python tools/regress.py -m patch   # 只跑改包",
        "```",
        "",
    ]
    counts = {m: len(by_module.get(m, [])) for m in MODULES}
    lines += ["| 模块 | 用例数 |", "|---|---|"]
    for m in MODULES:
        if counts[m]:
            lines.append(f"| {m} | {counts[m]} |")
    lines.append("")

    for m in MODULES:
        items = by_module.get(m)
        if not items:
            continue
        name = items[0].module if items else m
        lines += [f"## {name}（{len(items)} 条）", "",
                  "| 编号 | 级别 | 用例 | 验证点 |", "|---|---|---|---|"]
        for c in items:
            lines.append(f"| `{c.cid}` | {label.get(c.level, c.level)} | "
                         f"{c.title} | {'、'.join(c.tags) or '—'} |")
        lines.append("")

    lines += [
        "## 加用例的规矩",
        "",
        "1. 在对应模块的 `tests/test_<模块>.py` 里写函数，用 `@case(\"前缀NN\", ...)` 登记；",
        "2. 编号在模块内递增，**已发布的编号不复用**（删了就留空号）；",
        "3. 标题写「用户能感知的行为」，不写「测试某函数」；",
        "4. `tags` 填这条用例真正守住的东西（如「物理列名不变」），回归失败时一眼看出影响面；",
        "5. 断言要基于**真实产出**（解 zip、读 XML），不要只断言中间变量；",
        "6. 加完跑 `./.venv/bin/python tools/regress.py -m <模块>`，再 `--report` 更新本文件。",
        "",
        "### 新功能迁入时的最小动作",
        "",
        "- 新增一个 HTTP 接口 → 在 `test_webui.py` 补一条「正常返回」+ 一条「坏输入 400」；",
        "- 新增一种字段类型 → 在 `test_bo.py` 与 `test_form.py` 各补一条，断言 XML 里的真实属性；",
        "- 改了生成逻辑 → 先跑全量确认无退化，再把新行为补成用例；",
        "- 只能人工确认的（连库、大模型）→ 用 `skip(\"原因\")`，并在验收单里注明手工步骤。",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"清单已写入 {path.relative_to(ROOT)}（{len(cases)} 条）")


def main() -> int:
    ap = argparse.ArgumentParser(description="PP6 Studio 回归测试")
    ap.add_argument("--list", action="store_true", help="只列用例，不执行")
    ap.add_argument("-m", "--module", default="", help="只跑某模块")
    ap.add_argument("--only", default="", help="按编号前缀过滤，如 BO / HTTP")
    ap.add_argument("--level", default="", help="只跑某级别：P0/P1/P2")
    ap.add_argument("-x", "--fail-fast", action="store_true", help="首个失败即停")
    ap.add_argument("--report", default="", help="把用例清单写成 markdown")
    args = ap.parse_args()

    load()
    try:
        return _run(args)
    finally:
        # 所有出口（全跑 / 只列清单 / 写报告 / 过滤后没用例 / 首个失败即停）都收干净，
        # 不给 /tmp 留 pp6kit-tmp-* 目录
        _cleanup_tmp()


def _run(args) -> int:
    cases = H.REGISTRY
    if not cases:
        print("没有收集到用例：检查 tests/test_*.py 是否存在")
        return 1

    if args.report:
        write_report(ROOT / args.report, cases)
        return 0

    chosen = select(cases, args.only, args.module, args.level)
    if not chosen:
        print("过滤后没有用例")
        return 1

    if args.list:
        width = max(len(c.cid) for c in chosen)
        last = None
        for c in chosen:
            if c.module != last:
                print(f"\n[{c.module}]")
                last = c.module
            print(f"  {c.cid:<{width}} [{c.level}] {c.title}")
        print(f"\n共 {len(chosen)} 条")
        return 0

    print(f"PP6 Studio 回归 · {len(chosen)} 条\n")
    ok, bad, _ = run(chosen, args.fail_fast)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
