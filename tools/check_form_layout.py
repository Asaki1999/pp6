"""表单结构自检：生成的 XML 是不是一套「布局 V1.0」。

系统里表单是两层结构（card/fieldset → grid_layout → 控件），而且
grid_layout 的 fieldOptions 要把同一批单元格写两份（columns + rows）。
这三处 id 引用只要有一处对不上，导入后设计器里就是空白或错位，
所以出包前后都值得跑一遍。

检查项：
  1. parentId 是否都指向存在的元素
  2. columns 单元格数 == rows 单元格总数
  3. 每个单元格正好 1 个控件，且该控件 parentId == 单元格 id
  4. rows 里的控件 JSON 带 field_name / icon
  5. columns[].fields[0] 指向的控件确实挂在同一个 grid_layout 下
  6. 控件本体与 rows 内嵌 JSON 的 field_type / field_options 完全一致
  7. 同一 grid_layout 下控件 sn 从 1 连续

用法：
    python tools/check_form_layout.py <表单zip或目录> [...]
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

CONTAINERS = {"grid_layout", "card", "fieldset", "hidden"}


def check(zp: Path) -> tuple[list[str], int, int]:
    if zp.suffix == ".xml":
        root = ET.parse(zp).getroot()
    else:
        with zipfile.ZipFile(zp) as z:
            names = [n for n in z.namelist() if n.endswith(".xml")]
            if not names:
                return ["包里没有 xml"], 0, 0
            root = ET.fromstring(z.read(names[0]))
    defs = root.find("formDefs")
    if defs is None:
        return ["不是表单包（没有 formDefs）"], 0, 0

    fields = defs.findall("formField")
    by_id = {f.get("id"): f for f in fields}
    bad: list[str] = []

    for f in fields:
        pid = f.get("parentId")
        if pid and pid not in by_id:
            bad.append(f"控件 {f.get('name')} 的 parentId 指向不存在的元素")

    grids = [f for f in fields if f.get("fieldType") == "grid_layout"]
    for g in grids:
        opts = json.loads(g.get("fieldOptions") or "{}")
        cells = [c for r in opts.get("rows") or [] for c in r.get("columns") or []]
        if len(opts.get("columns") or []) != len(cells):
            bad.append(f"{g.get('name')}: columns({len(opts.get('columns') or [])}) "
                       f"≠ rows 单元格({len(cells)})")
        for cell in cells:
            if len(cell.get("fields") or []) != 1:
                bad.append(f"{g.get('name')}: 单元格里不是正好 1 个控件")
            for w in cell.get("fields") or []:
                if w.get("parentId") != cell.get("id"):
                    bad.append(f"{g.get('name')}: {w.get('name')} 的 parentId ≠ 单元格 id")
                if not w.get("field_name"):
                    bad.append(f"{w.get('name')}: rows 里缺 field_name")
                if not w.get("icon"):
                    bad.append(f"{w.get('name')}: 缺 icon")
        for col in opts.get("columns") or []:
            el = by_id.get((col.get("fields") or [None])[0])
            if el is None or el.get("parentId") != g.get("id"):
                bad.append(f"{g.get('name')}: columns 引用了不属于本布局的控件")

        kids = [f for f in fields if f.get("parentId") == g.get("id")]
        by_name = {k.get("name"): k for k in kids}
        for cell in cells:
            for w in cell.get("fields") or []:
                el = by_name.get(w.get("name"))
                if el is None:
                    bad.append(f"{g.get('name')}: rows 里的 {w.get('name')} 没有对应 formField")
                    continue
                if el.get("fieldType") != w.get("field_type"):
                    bad.append(f"{w.get('name')}: 元素类型 {el.get('fieldType')} "
                               f"≠ rows {w.get('field_type')}")
                if json.loads(el.get("fieldOptions") or "{}") != w.get("field_options"):
                    bad.append(f"{w.get('name')}: field_options 两份不一致")
        sns = sorted(int(k.get("sn") or 0) for k in kids)
        if sns != list(range(1, len(kids) + 1)):
            bad.append(f"{g.get('name')}: sn 不连续 {sns}")

    n_field = len([f for f in fields if f.get("fieldType") not in CONTAINERS])
    return bad, len(grids), n_field


def main(argv: list[str]) -> int:
    targets: list[Path] = []
    for arg in argv:
        p = Path(arg)
        if p.is_dir():
            targets.extend(sorted(p.rglob("*.zip")) + sorted(p.rglob("*.xml")))
        else:
            targets.append(p)
    if not targets:
        print(__doc__)
        return 2

    failed = 0
    for zp in targets:
        bad, n_grid, n_field = check(zp)
        mark = "OK" if not bad else "✗"
        print(f"{mark} {zp.name:44s} 布局 {n_grid} 控件 {n_field}")
        for b in bad[:8]:
            print(f"     - {b}")
        failed += 1 if bad else 0
    print(f"\n{len(targets) - failed}/{len(targets)} 个包结构一致")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
