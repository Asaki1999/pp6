"""结构兼容性比对：系统导出包 vs 工具生成包。

不比字节、不比节点顺序，只比「结构契约」：
  1. 业务对象：按字段编码对齐，比对每个字段的属性名集合
  2. 表单：按控件 name 对齐，比对属性名集合
  3. 列表/数据集：按视图类型对齐

判定：
  ✗ 生成包里有、原始包里没有的结构  -> 说明凭空造了系统不认识的东西（高危）
  ! 原始包里有、生成包里没有的结构  -> 说明生成不全（视内容而定）

用法：
    python tools/compare.py <原始zip或目录> <生成zip或目录>
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# desc / format 在系统自带的导出里两种写法并存（有/无都合法），不计入差异
VOLATILE = {"permission", "extendAttr", "attrs", "displayColumns", "functionButtons",
            "extColumns", "sortColumns", "options", "resultColumns", "desc", "format"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    return next((c for c in el if _local(c.tag) == name), None)


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _load(path: Path) -> dict[str, ET.Element]:
    out: dict[str, ET.Element] = {}
    targets = [path] if path.is_file() else sorted(path.rglob("*.zip"))
    for zp in targets:
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                if not name.endswith(".xml") or name.startswith("__MACOSX"):
                    continue
                key = Path(name).stem.split("-", 1)[-1]
                if key in out:
                    continue
                try:
                    out[key] = ET.fromstring(z.read(name))
                except ET.ParseError:
                    continue
    return out


def _bo_fp(root) -> dict[str, set[str]]:
    ent = _child(root, "bodefentity")
    if ent is None:
        return {}
    return {
        a.attrib.get("code", "?"): set(a.attrib) - VOLATILE for a in _children(ent, "attribute")
    }


CONTAINERS = {"grid", "grid_layout", "card", "fieldset", "tabs", "tablelayout"}


def _form_fp(root) -> tuple[dict[str, set[str]], dict[str, int]]:
    """表单指纹 -> (控件指纹, 布局容器计数)。

    容器（栅格/表格布局/卡片/分组框/标签页）**只比用了哪几种、各几个**，
    不比 id 和 name——这些名字是随机的（grid_xxxxx / card_xxxxx），
    而且「用什么容器」本身就是生成策略，不是系统契约。
    真正要严比的是控件：控件的 name 对应业务字段，凭空造一个系统不认识。
    """
    defs = _child(root, "formDefs")
    if defs is None:
        return {}, {}
    out: dict[str, set[str]] = {}
    containers: dict[str, int] = {}
    for f in _children(defs, "formField"):
        ftype = f.attrib.get("fieldType", "?")
        name = f.attrib.get("name", "?")
        if ftype in CONTAINERS:
            key = "grid" if ftype in ("grid", "grid_layout") else ftype
            containers[key] = containers.get(key, 0) + 1
            continue
        if ftype == "hidden" and name in ("id", "version", "pinyin"):
            continue  # 系统内置隐藏控件，有/无可配，属预期差异
        out.setdefault(f"{ftype}:{name}", set()).update(set(f.attrib) - VOLATILE)
    return out, containers


def _tpl_fp(root) -> dict[str, set[str]]:
    defs = _child(root, "dataTemplateDefs")
    if defs is None:
        return {}
    out = {}
    for t in _children(defs, "dataTemplateTpl"):
        try:
            view = json.loads(t.attrib.get("attrs", "{}")).get("template_renderview_type", "tpl")
        except json.JSONDecodeError:
            view = "tpl"
        if "resultColumns" in t.attrib:
            view = "value_source"
        out[f"tpl:{view}"] = set(t.attrib) - VOLATILE
    for d in _children(defs, "dataTemplateDef"):
        out["def:" + d.attrib.get("type", "?")] = set(d.attrib) - VOLATILE
    return out


def _compare_group(title: str, left: dict[str, set[str]], right: dict[str, set[str]]) -> int:
    invented = sorted(set(right) - set(left))
    missing = sorted(set(left) - set(right))
    attr_diff = [(k, left[k] - right[k], right[k] - left[k])
                 for k in sorted(set(left) & set(right)) if left[k] != right[k]]
    if not (invented or attr_diff):
        print(f"  ✓ {title}: {len(right)} 项，结构一致"
              + (f"（原始多 {len(missing)} 项：{missing[:4]}）" if missing else ""))
        return 0

    print(f"  ✗ {title}")
    if invented:
        print(f"      生成独有（原始没有，需确认系统是否接受）: {invented[:6]}")
    for key, lack, extra in attr_diff[:8]:
        detail = []
        if lack:
            detail.append(f"生成缺 {sorted(lack)}")
        if extra:
            detail.append(f"生成多 {sorted(extra)}")
        print(f"      {key}: {' / '.join(detail)}")
    if len(attr_diff) > 8:
        print(f"      ... 另有 {len(attr_diff)-8} 项属性差异")
    return len(invented) + len(attr_diff)


def _compare_form(title: str, left_root, right_root) -> int:
    l_fields, l_cont = _form_fp(left_root)
    r_fields, r_cont = _form_fp(right_root)
    problems = _compare_group(title, l_fields, r_fields)
    if l_cont != r_cont:
        print(f"      · 布局容器 原始{sorted(l_cont.items()) or '无'} → 生成{sorted(r_cont.items())}"
              f"（容器选型属生成策略，不计为差异）")
    return problems


def compare(a: Path, b: Path) -> int:
    left, right = _load(a), _load(b)
    print(f"原始 {len(left)} 个文件，生成 {len(right)} 个文件\n")
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    if only_left:
        print(f"  仅原始: {only_left}")
    if only_right:
        print(f"  仅生成: {only_right}")

    problems = 0
    for key in sorted(set(left) & set(right)):
        root_l, root_r = left[key], right[key]
        if _local(root_l.tag) == "bodefs":
            problems += _compare_group(f"[业务对象] {key}", _bo_fp(root_l), _bo_fp(root_r))
        elif _local(root_l.tag) == "form":
            problems += _compare_form(f"[表单] {key}", root_l, root_r)
        else:
            problems += _compare_group(f"[列表/数据集] {key}", _tpl_fp(root_l), _tpl_fp(root_r))

    ok = problems == 0 and not only_left and not only_right
    print(f"\n结论: {'结构兼容' if ok else f'{problems} 处需确认'}")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    raise SystemExit(compare(Path(sys.argv[1]), Path(sys.argv[2])))
