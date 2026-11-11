"""从系统导出的样例 zip 中提取「骨架片段」，产出 assets/snippets.json。

目的：生成 XML 时以真实导出结果为模板，只替换差异字段，
保证控件参数、视图参数与新系统版本保持兼容（升级系统后重跑本脚本即可）。

用法：
    python tools/capture_snippets.py <样例目录>  [-o assets/snippets.json]
"""
from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

DIR_FORM = "form"
DIR_TPL = "data_template"


def _iter_xml(root: Path):
    for zp in sorted(root.rglob("*.zip")):
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                if not name.endswith(".xml") or name.startswith("__MACOSX"):
                    continue
                kind = name.split("/")[0]
                key = Path(name).stem.split("-", 1)[-1]
                try:
                    el = ET.fromstring(z.read(name))
                except ET.ParseError:
                    continue
                yield kind, key, el, zp


def capture(root: Path) -> dict:
    snip: dict = {
        "formDefExtendAttr": "",
        "fieldOptions": {},   # 控件类型 -> fieldOptions JSON 字符串
        "grid": {},           # root / row1 / row2 -> fieldOptions
        "layout": {},         # card / fieldset / grid_layout -> fieldOptions（外层参数）
        "def": {},            # 数据集类型 -> dataTemplateDef 全属性
        "tpl": {},            # 视图类型 -> dataTemplateTpl 全属性
        "dataSet": {},        # dataSet 全属性（值来源/筛选器）
        "column": {},         # 单个列定义骨架
    }

    for kind, key, el, src in _iter_xml(root):
        if kind == DIR_FORM:
            defs = el.find("formDefs")
            if defs is None:
                continue
            fd = defs.find("formDef")
            if fd is not None and not snip["formDefExtendAttr"]:
                snip["formDefExtendAttr"] = fd.attrib.get("extendAttr", "")

            for ff in defs.findall("formField"):
                ft = ff.attrib.get("fieldType")
                opts = ff.attrib.get("fieldOptions")
                if not ft or opts is None:
                    continue
                name = ff.attrib.get("name", "")
                # 分区容器与表格布局V1.0：只留外层参数。
                # 几何结构（columns / rows 单元格）由生成器按字段重新算，
                # 卡片里的 extra 是「删除项目」这类工具栏，不能带进生成的包。
                if ft in ("card", "fieldset"):
                    try:
                        o = json.loads(opts)
                    except json.JSONDecodeError:
                        continue
                    o.pop("extra", None)
                    o["columns"] = [{"name": None, "label": None, "fields": [], "span": ""}]
                    snip["layout"].setdefault(ft, o)
                    continue
                if ft == "grid_layout":
                    try:
                        o = json.loads(opts)
                    except json.JSONDecodeError:
                        continue
                    o["columns"], o["rows"] = [], []
                    snip["layout"].setdefault("grid_layout", o)
                    continue
                if ft == "grid":
                    try:
                        o = json.loads(opts)
                    except json.JSONDecodeError:
                        continue
                    cols = o.get("columns") or []
                    if o.get("is_kit"):
                        snip["grid"].setdefault("root", opts)
                    elif len(cols) == 1:
                        snip["grid"].setdefault("row1", opts)
                    elif len(cols) == 2:
                        snip["grid"].setdefault("row2", opts)
                    continue
                if ft == "hidden":
                    snip["fieldOptions"].setdefault("_hidden", opts)
                    if name in ("id", "version", "pinyin"):
                        snip["fieldOptions"].setdefault(f"_hidden_{name}", opts)
                    continue
                snip["fieldOptions"].setdefault(ft, opts)

        elif kind == DIR_TPL:
            defs = el.find("dataTemplateDefs")
            if defs is None:
                continue
            for node in defs:
                if node.tag == "dataTemplateDef":
                    t = node.attrib.get("type", "default")
                    snip["def"].setdefault(t, dict(node.attrib))
                elif node.tag == "dataTemplateTpl":
                    try:
                        attrs = json.loads(node.attrib.get("attrs", "{}"))
                    except json.JSONDecodeError:
                        attrs = {}
                    view = attrs.get("template_renderview_type", "grid")
                    # 值来源模板用 resultColumns 而不是 displayColumns
                    if "displayColumns" not in node.attrib and "resultColumns" in node.attrib:
                        view = "value_source"
                    snip["tpl"].setdefault(view, dict(node.attrib))
                    dc = node.attrib.get("displayColumns")
                    if dc and not snip["column"]:
                        try:
                            cols = json.loads(dc)
                        except json.JSONDecodeError:
                            cols = []
                        if cols:
                            snip["column"] = cols[0]
                elif node.tag == "dataSet" and not snip["dataSet"]:
                    snip["dataSet"] = dict(node.attrib)

    snip["_meta"] = {
        "source": str(root),
        "fieldTypes": sorted(k for k in snip["fieldOptions"] if not k.startswith("_")),
        "grids": sorted(snip["grid"]),
        "layouts": sorted(snip["layout"]),
        "views": sorted(snip["tpl"]),
        "defTypes": sorted(snip["def"]),
        "hasDataSet": bool(snip["dataSet"]),
    }
    return snip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sample_dir", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("assets/snippets.json"))
    a = ap.parse_args()

    snip = capture(a.sample_dir)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(snip, ensure_ascii=False, indent=1), encoding="utf-8")

    m = snip["_meta"]
    print(f"✓ 已生成 {a.out}")
    print(f"  控件类型 {len(m['fieldTypes'])}: {', '.join(m['fieldTypes'])}")
    print(f"  栅格模板: {m['grids']}")
    print(f"  分区容器/布局: {m.get('layouts') or '—（样例里没有 card/fieldset/grid_layout）'}")
    print(f"  列表视图: {m['views']}")
    print(f"  数据集类型: {m['defTypes']}  (dataSet: {m['hasDataSet']})")
    for need in ("text", "select", "grid"):
        if need not in str(m):
            print(f"  ⚠ 骨架缺失: {need}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
