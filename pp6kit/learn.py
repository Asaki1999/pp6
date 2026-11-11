"""从真实导出包里校准字段类型字典。

扫描业务对象 XML 里的 attribute，按 dataTypeNew 归集实际的
datatype / length / format，回写 assets/fieldtypes.json。

用法：
    bin/pp6 learn <导出zip或目录>
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .resources import resource_root


def _iter_attributes(path: Path):
    targets = [path] if path.is_file() else sorted(path.rglob("*.zip"))
    for zp in targets:
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                if not name.endswith(".xml") or name.split("/")[0] != "bo":
                    continue
                try:
                    root = ET.fromstring(z.read(name))
                except ET.ParseError:
                    continue
                for el in root.iter():
                    if el.tag.rsplit("}", 1)[-1] == "attribute" and el.attrib.get("dataTypeNew"):
                        yield el.attrib


def collect(path: Path) -> dict[str, dict]:
    seen: dict[str, dict] = {}
    for a in _iter_attributes(path):
        t = a["dataTypeNew"]
        bucket = seen.setdefault(t, {"count": 0, "datatype": {}, "length": {}, "format": {}, "objectType": 0})
        bucket["count"] += 1
        bucket["datatype"][a.get("datatype", "")] = bucket["datatype"].get(a.get("datatype", ""), 0) + 1
        if a.get("length"):
            bucket["length"][a["length"]] = bucket["length"].get(a["length"], 0) + 1
        if a.get("format"):
            bucket["format"][a["format"]] = bucket["format"].get(a["format"], 0) + 1
        if a.get("objectType"):
            bucket["objectType"] += 1
    return seen


def _top(counter: dict, default=None):
    if not counter:
        return default
    return max(counter.items(), key=lambda kv: kv[1])[0]


def learn(path: Path) -> tuple[int, int, list[str]]:
    """返回 (已校准数, 新增数, 说明行)。"""
    found = collect(path)
    dict_path = resource_root() / "assets" / "fieldtypes.json"
    data = json.loads(dict_path.read_text(encoding="utf-8"))
    types: dict = data.setdefault("types", {})

    lines: list[str] = []
    verified = added = 0

    for code, info in sorted(found.items()):
        datatype = _top(info["datatype"], "varchar")
        entry = types.get(code)
        if entry is None:
            types[code] = {
                "label": code,
                "datatype": datatype,
                "length": int(_top(info["length"], 0)) or None,
                "format": _top(info["format"]),
                "verified": True,
                "note": "由 learn 自动收录，中文名请人工补全",
            }
            added += 1
            lines.append(f"  + 新类型 {code}（{info['count']} 个字段，datatype={datatype}）→ 请补 label")
            continue

        # 只学结构性信息：datatype 以真实导出为准；
        # length/format 是可调的业务默认值，缺了才补，绝不覆盖已有配置
        before = (entry.get("datatype"), entry.get("length"), entry.get("format"))
        entry["datatype"] = datatype
        if entry.get("length") is None:
            entry["length"] = int(_top(info["length"], 0)) or None
        if entry.get("format") is None:
            entry["format"] = _top(info["format"])
        if entry.get("verified"):
            entry.pop("note", None)
        entry["verified"] = True
        after = (entry.get("datatype"), entry.get("length"), entry.get("format"))
        verified += 1
        mark = "  ✓" if before == after else "  ~"
        detail = "" if before == after else f"  {before} → {after}"
        lines.append(f"{mark} {code:14} {info['count']:>3} 个字段{detail}")

    dict_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return verified, added, lines
