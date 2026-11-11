"""端到端自检：每次系统升级后跑一遍，确认生成器没有跟系统格式脱节。

检查项：
  1. 骨架可重建（capture）
  2. 系统导出包可反解为 Excel（inspect）且校验 0 错误
  3. 反解结果能重新出包（build），XML 全部合法、ID 无重复
  4. 重出的包与原始导出包结构兼容（compare）
  5. 空白模板能出包（新对象场景）

用法：
    python tools/selftest.py [样例目录]
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
SAMPLE = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "PP6"

results: list[tuple[str, bool, str]] = []


def run(args: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run([PY, *args], cwd=cwd, capture_output=True, text=True)


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))


def iter_xml(zp: Path):
    """产出 (名字, 字节)。模块包里的条目现在是 XML 原文，直接产出即可。"""
    with zipfile.ZipFile(zp) as z:
        for name in z.namelist():
            if name.endswith(".xml"):
                yield name, z.read(name)


def check_xml(base: Path) -> tuple[bool, str]:
    bad, total, ids = [], 0, {}
    dup = []
    # 只用「按模块」下的包做校验：同一份 XML 在 bo/、form/ 里也各有一份单条包，
    # 两边都扫会把同一批 ID 数两遍，误报重复。
    packed = sorted((base / "按模块").glob("*.zip"))
    for zp in (packed or sorted(base.rglob("*.zip"))):
        for name, raw in iter_xml(zp):
            total += 1
            with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as fh:
                fh.write(raw)
                tmp = Path(fh.name)
            r = subprocess.run(["xmllint", "--noout", str(tmp)], capture_output=True, text=True)
            tmp.unlink()
            if r.returncode != 0:
                bad.append(f"{zp.name}/{name}: {r.stderr.strip()[:120]}")
            for m in re.finditer(rb'\bid="(\d{15,})"', raw):
                key = m.group(1)
                if key in ids:
                    dup.append(key.decode())
                ids[key] = True
    ok = not bad and not dup
    detail = f"{total} 个 XML"
    if bad:
        detail += f"，非法 {len(bad)}: {bad[:2]}"
    if dup:
        detail += f"，ID 重复 {len(dup)}"
    return ok, detail


def main() -> int:
    if not SAMPLE.exists():
        print(f"样例目录不存在: {SAMPLE}")
        return 2
    work = Path(tempfile.mkdtemp(prefix="pp6-selftest-"))
    print(f"工作目录: {work}\n")

    # 1 骨架
    snip = work / "snippets.json"
    r = run(["tools/capture_snippets.py", str(SAMPLE), "-o", str(snip)])
    step("骨架重建", r.returncode == 0, (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else "")

    # 2 反解
    xlsx = work / "反解.xlsx"
    r = run(["-m", "pp6kit", "--snippets", str(snip), "inspect", str(SAMPLE), "-o", str(xlsx)])
    step("导出包反解", r.returncode == 0 and xlsx.exists(), (r.stdout or r.stderr).strip().splitlines()[0])

    # 3 校验
    r = run(["-m", "pp6kit", "--snippets", str(snip), "check", str(xlsx)])
    errors = re.search(r"(\d+) 个错误", r.stdout or "")
    step("反解结果校验", r.returncode == 0, errors.group(0) if errors else "")

    # 4 出包
    out = work / "build"
    r = run(["-m", "pp6kit", "--snippets", str(snip), "build", str(xlsx), "-o", str(out)])
    built = r.returncode == 0 and any(out.rglob("*.zip"))
    step("重新出包", built, f"{len(list(out.rglob('*.zip')))} 个 zip" if built else (r.stdout or r.stderr)[-200:])

    if built:
        ok, detail = check_xml(out)
        step("XML 合法 & ID 唯一", ok, detail)

        r = run(["tools/compare.py", str(SAMPLE), str(out)])
        step("与原导出包结构兼容", r.returncode == 0, (r.stdout or "").strip().splitlines()[-1])

    # 5 模板 -> 出包
    tpl = work / "模板.xlsx"
    r = run(["-m", "pp6kit", "--snippets", str(snip), "template", str(tpl)])
    tpl_out = work / "tpl_build"
    r2 = run(["-m", "pp6kit", "--snippets", str(snip), "build", str(tpl), "-o", str(tpl_out)])
    ok = tpl.exists() and r2.returncode == 0 and any(tpl_out.rglob("*.zip"))
    if ok:
        ok2, detail = check_xml(tpl_out)
        step("空白模板出包", ok and ok2, detail)
    else:
        step("空白模板出包", False, (r2.stdout or r2.stderr)[-200:])

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} 项通过")
    if passed != len(results):
        print("工作目录保留以便排查: " + str(work))
        return 1
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
